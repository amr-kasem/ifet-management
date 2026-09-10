"""Turn a mirrored Airtable hierarchy into a LabOS project. Once, safely.

Three properties, and each is a decision rather than an implementation detail.

**It reads the mirror, never Airtable.** Import is an operator action at a rig;
it cannot depend on someone else's API being reachable. If the mirror is stale
the import is stale and says so, which is a fact the operator can act on — a
timeout is not.

**It goes through the same create path as manual entry.** Not a parallel one.
Plan §4.6: *"Creating a project always goes through the same
`POST /devices/{id}/projects/` whether the values were typed or pre-filled.
Standalone and synced are one form with the boxes empty or filled — not two
flows."* A second create path is how the two modes drift until one of them is
subtly wrong, and the six static and eight cyclic tests derived from the design
pressures are exactly what would drift.

**It is duplicate-safe on the mock-up record.** A repeated import returns the
project it already made. The mock-up is the right key because that is what a
LabOS project *is* — one specimen — and because an operator re-importing after
a refresh is a normal action, not an error to be punished with a second set of
tests against the same specimen.
"""

import logging

from . import requirements as req
from .mirror import (AtMirrorProject, AtMirrorProtocol, AtMirrorSection,
                     AtMirrorSpecimen)

log = logging.getLogger("app.airtable.importer")

# Which local test type each executable requirement code becomes. `IMPACT_SMI`
# and `IMPACT_LMI` are both missile impact — small and large missile are the
# same workflow with a different missile, which is why the missile is a
# requirement field and not a test type.
LOCAL_TYPE_BY_CODE = {
    "STATIC_PRESSURE": "static",
    "CYCLIC_PRESSURE": "cyclic",
    "IMPACT_LMI": "impact",
    "IMPACT_SMI": "impact",
    "FORCED_ENTRY": "manual:Forced Entry",
    "ANSI_IMPACT": "manual:ANSI Z97.1",
}

# Static programmes LabOS can actually run. Contract §3.1 already says the
# "initial static programme supports `Full`" — until 2026-09-10 nothing
# enforced it, and an unsupported value was discarded silently.
SUPPORTED_STATIC_PROGRAMMES = frozenset({"Full"})

# Airtable owns the impact family through the requirement code. The operator
# is never asked SMI vs LMI for a bound test, so the two cannot disagree.
IMPACT_FAMILY_BY_CODE = {"IMPACT_SMI": "SMI", "IMPACT_LMI": "LMI"}


class ImportError_(Exception):
    """The hierarchy cannot be imported as given."""


class ImportPlan:
    """What an import would do, before it does any of it.

    Built and returned separately so a caller can show an operator the
    consequences — which sections are executable, which are unconfirmed, which
    are refused and why — before anything is written. An import that silently
    skipped a malformed section would look identical to one where every section
    was fine.
    """

    def __init__(self, project, specimen, protocol, sections):
        self.project = project
        self.specimen = specimen
        self.protocol = protocol
        self.sections = sections
        self.executable = []       # (code, section, local_type)
        self.not_required = []
        self.unconfirmed = []
        self.refused = []          # (section, reason)
        self.design_pressures = None
        self.gauge_count = None
        self.impact_count = None
        self.static_unavailable = None   # why Static Load cannot run, if it cannot

    def as_dict(self):
        return {
            "project": {"record_id": self.project.record_id,
                        "job_number": self.project.job_number,
                        "mirrored_at": _iso(self.project.mirrored_at)},
            "specimen": {"record_id": self.specimen.record_id,
                         "name": self.specimen.specimen_name},
            "protocol": {"record_id": self.protocol.record_id,
                         "name": self.protocol.protocol_name},
            "design_pressures": self.design_pressures,
            "gauge_count": self.gauge_count,
            "impact_count": self.impact_count,
            "static_unavailable": self.static_unavailable,
            "executable": [{"code": c, "section": s.record_id,
                            "section_name": s.section_name,
                            "local_type": t}
                           for c, s, t in self.executable],
            "not_required": [s.record_id for s in self.not_required],
            "unconfirmed": [s.record_id for s in self.unconfirmed],
            "refused": [{"section": s.record_id,
                         "section_name": s.section_name,
                         "reason": str(r)} for s, r in self.refused],
        }


def _iso(value):
    return value.isoformat() if value is not None else None


def plan(session, project_record_id, specimen_record_id, protocol_record_id):
    """Read the mirror and work out what this hierarchy asks for. Writes nothing.

    Refuses a hierarchy whose parents do not actually link to each other:
    importing a protocol that belongs to a different specimen would attach a
    result to the wrong mock-up, and every downstream identifier would be
    plausible.
    """
    project = session.get(AtMirrorProject, project_record_id)
    specimen = session.get(AtMirrorSpecimen, specimen_record_id)
    protocol = session.get(AtMirrorProtocol, protocol_record_id)
    missing = [n for n, v in (("project", project), ("specimen", specimen),
                              ("protocol", protocol)) if v is None]
    if missing:
        raise ImportError_(
            f"{missing} are not in the local mirror. Refresh the mirror first — "
            "import reads the mirror and never calls Airtable, so an operator's "
            "import cannot fail because someone else's API is down.")

    if specimen.project_record_id and specimen.project_record_id != project_record_id:
        raise ImportError_(
            f"specimen {specimen_record_id} belongs to project "
            f"{specimen.project_record_id}, not {project_record_id}. Importing "
            "a mismatched hierarchy would attach results to the wrong mock-up.")
    if protocol.specimen_record_id and protocol.specimen_record_id != specimen_record_id:
        raise ImportError_(
            f"protocol {protocol_record_id} belongs to specimen "
            f"{protocol.specimen_record_id}, not {specimen_record_id}.")

    sections = (session.query(AtMirrorSection)
                .filter(AtMirrorSection.protocol_record_id == protocol_record_id)
                .order_by(AtMirrorSection.record_id).all())
    if not sections:
        raise ImportError_(
            f"protocol {protocol_record_id} has no Protocol Sections in the "
            "mirror, so there is nothing to import.")

    out = ImportPlan(project, specimen, protocol, sections)
    programmes = []
    for section in sections:
        applicability = req.applicability_of(section)
        try:
            kind = req.validate(section)
        except req.RequirementError as exc:
            # Refused, and recorded — not skipped. A malformed section must be
            # visible, because the operator is the only one who can fix it.
            out.refused.append((section, exc))
            continue

        code = section.requirement_code
        if code == "GAUGE_COUNT":
            out.gauge_count = int(section.required_value)
            continue
        if code == "STATIC_PROGRAMME":
            # A parameter, not a test — but not one that can be ignored.
            # Held for the post-loop gate below: a protocol's STATIC_PRESSURE
            # sections may already have been read by the time this one is
            # seen, so suppression cannot be decided inline.
            programmes.append(section)
            continue
        if code in ("STATIC_PRESSURE", "CYCLIC_PRESSURE"):
            pair = (section.required_value_inward, section.required_value_outward)
            if out.design_pressures and out.design_pressures != list(pair):
                out.refused.append((section, req.RequirementError(
                    f"this section requires {list(pair)} PSF but another "
                    f"section of the same protocol requires "
                    f"{out.design_pressures}. LabOS derives all fourteen stages "
                    "from one pair, so two disagreeing pairs cannot both hold.")))
                continue
            out.design_pressures = list(pair)
        if code in ("IMPACT_LMI", "IMPACT_SMI"):
            # **Accumulated, not assigned.** This was `out.impact_count = ...`,
            # which is last-section-wins: a protocol requiring 2 large-missile
            # impacts and 3 small-missile ones ended up with whichever section
            # the mirror happened to return last, and both values are
            # individually plausible.
            #
            # Refusing on disagreement — the rule directly above for the design
            # pressures — would be wrong here. Two pressure sections that
            # disagree cannot both hold, because all fourteen stages come from
            # one pair. Two impact sections that differ are **not** a
            # contradiction: LMI and SMI are different missiles, each with its
            # own required count, and `bind` creates a separate impact test for
            # each. So the project-level figure is the total number of impacts
            # the protocol requires, and the per-test figure is the section's
            # own — read from `section.required_value` where the test is built.
            out.impact_count = (out.impact_count or 0) + int(section.required_value)

        if applicability == req.NOT_REQUIRED:
            out.not_required.append(section)
            continue
        if code not in req.EXECUTABLE_CODES:
            continue
        if applicability == req.UNCONFIRMED:
            # Not executed, and not silently dropped. Blank means nobody has
            # said yet, which is a different state from "not needed".
            out.unconfirmed.append(section)
            continue
        out.executable.append((code, section, LOCAL_TYPE_BY_CODE[code]))

    _gate_static_programme(out, programmes)
    return out


def _gate_static_programme(out, programmes):
    """An unsupported static programme makes Static Load non-executable.

    **Report-only refusal would not have worked, and that is the point.**
    `refused` is serialised into the import report by `as_dict` and consumed by
    nothing. Appending to it and stopping would have reported the unsupported
    programme and then run the **Full** six-stage sequence anyway — LabOS
    derives the programme from the design-pressure pair and never consulted
    this value at all. The operator would have seen an ordinary static test.

    So the suppression is narrow and explicit: **Static Load only.** Cycles,
    Impact, Forced Entry and ANSI Z97.1 in the same protocol are untouched, and
    `design_pressures` is deliberately left set — Cycles derives its eight
    stages from the same pair and has no programme of its own.

    A blank `Required Option` never reaches here: `requirements.validate`
    already refuses an Enum requirement with no option, so that section is
    refused in the loop above.
    """
    for section in programmes:
        option = (section.required_option or "").strip()
        if option in SUPPORTED_STATIC_PROGRAMMES:
            continue

        supported = ", ".join(sorted(SUPPORTED_STATIC_PROGRAMMES))
        out.refused.append((section, req.RequirementError(
            f"static programme {option!r} is not supported. LabOS runs "
            f"{supported} only, and will not substitute the Full programme for "
            "the one the proposal asked for. Static Load is not executable for "
            "this protocol; Cycles, Impact, Forced Entry and ANSI Z97.1 are "
            "unaffected.")))

        suppressed = [e for e in out.executable if e[0] == "STATIC_PRESSURE"]
        out.executable = [e for e in out.executable if e[0] != "STATIC_PRESSURE"]
        for _code, pressure_section, _type in suppressed:
            out.refused.append((pressure_section, req.RequirementError(
                f"Static Load is unavailable: this protocol asks for the "
                f"{option!r} static programme, which LabOS does not support. "
                "The design pressures themselves are readable and are not in "
                "doubt — what LabOS cannot do is run the programme requested.")))

        out.static_unavailable = (
            f"static programme {option!r} is not supported (LabOS runs "
            f"{supported} only)")


def existing_project(session, specimen_record_id):
    """The project already imported for this mock-up, or None.

    **The duplicate-safety key.** A LabOS project *is* one specimen, so the
    mock-up record is what identifies it. Re-importing after a mirror refresh is
    a normal operator action; answering it with a second project — and a second
    set of six static and eight cyclic tests against the same specimen — would
    turn a refresh into duplicated certification work.
    """
    from ..data.models import Project
    return (session.query(Project)
            .filter(Project.airtable_mockup_id == specimen_record_id)
            .first())


def bind(session, project, import_plan):
    """Attach the Airtable identity to the project and its generated tests.

    Runs **after** the shared create path has generated the six static and eight
    cyclic tests from the design pressures. Each executable section is bound to
    the tests of its own type, so a published result carries the section that
    actually specifies it — the alternative, one section for the whole project,
    would publish a Cycles result against the Forced Entry requirement.

    Static and cyclic have six and eight tests to one section: the section
    specifies the design pressure and LabOS derives every stage from it, so all
    of a type's tests share that section by construction.
    """
    from ..data.models import (CyclicTest, ManualTest, MissileImpactTest,
                               StaticTest)

    project.airtable_project_id = import_plan.project.record_id
    project.airtable_mockup_id = import_plan.specimen.record_id
    project.airtable_mockup_name = import_plan.specimen.specimen_name
    if import_plan.gauge_count is not None:
        project.gauge_count = import_plan.gauge_count
    if import_plan.impact_count is not None:
        project.impact_count = import_plan.impact_count

    protocol_id = import_plan.protocol.record_id
    created = []
    for code, section, local_type in import_plan.executable:
        common = dict(airtable_protocol_id=protocol_id,
                      airtable_section_id=section.record_id,
                      airtable_section_name=section.section_name)
        if local_type == "static":
            for test in project.static_tests:
                for k, v in common.items():
                    setattr(test, k, v)
        elif local_type == "cyclic":
            for test in project.cyclic_tests:
                for k, v in common.items():
                    setattr(test, k, v)
        elif local_type == "impact":
            # **The family is frozen here, once, and never again.** The import
            # route returns early on an existing project (`existing_project`),
            # so `bind` does not re-run on a refresh: this assignment happens
            # exactly at first import and nothing else in the system writes
            # the column. That is what makes the operator unable to contradict
            # Airtable - not a rule the API defends, but a value they are
            # never offered.
            #
            # Frozen deliberately. If the section's code later changes from
            # IMPACT_LMI to IMPACT_SMI upstream, this test keeps what it was
            # imported as, for the same reason `requirements.snapshot` freezes
            # the rest of the requirement: a finished test must go on claiming
            # what it actually ran against.
            test = MissileImpactTest(project_id=project.id,
                                     missile=section.missile,
                                     missile_weight=section.missile_weight,
                                     impact_family=IMPACT_FAMILY_BY_CODE[code],
                                     **common)
            session.add(test)
            created.append(("impact", test))
        elif local_type.startswith("manual:"):
            test = ManualTest(project_id=project.id,
                              type=local_type.split(":", 1)[1],
                              required_option=section.required_option,
                              finished=False, **common)
            session.add(test)
            created.append(("manual", test))
    return created


def prefill_values(import_plan, name=None):
    """The `ProjectCreateSchema` payload this hierarchy pre-fills.

    Returned as the same dict a person would have typed, which is the whole
    point: the create route cannot tell the difference, so there is one code
    path and one set of tests derived from it.

    Refuses without a design-pressure pair. Every static and cyclic stage is
    derived from it, so a project created without one has fourteen stages
    LabOS cannot compute — and defaulting the pair would invent a requirement.
    """
    if not import_plan.design_pressures:
        raise ImportError_(
            "this protocol has no usable design-pressure pair, so the fourteen "
            "static and cyclic stages cannot be derived. A default would be an "
            "invented requirement; the pair has to come from the proposal.")
    inward, outward = import_plan.design_pressures
    return {
        "name": name or import_plan.specimen.specimen_name or
                import_plan.specimen.record_id,
        "inward_design_pressure": float(inward),
        "outward_design_pressure": float(outward),
        "has_water_infiltration": False,
    }


def freeze_requirement(session, attempt, test):
    """Snapshot the requirement onto the attempt, at the moment it starts.

    **Why freezing is not optional.** The mirror is refreshed, and upstream
    requirements change — the Airtable team edits a section, someone corrects a
    proposal. An attempt that read its requirement at publish time would then
    report having been run against whatever the requirement says *now*, which
    for a test that has already physically happened is a false statement. The
    requirement is part of the evidence, so it is captured with the rest of it.

    Reads the mirrored section, so the snapshot is of what LabOS actually acted
    on. Absent when the test has no Airtable origin — a local job has no
    upstream requirement to freeze, which is a normal state.
    """
    section_id = getattr(test, "airtable_section_id", None)
    if not section_id:
        return None
    section = session.get(AtMirrorSection, section_id)
    if section is None:
        return None
    snap = req.snapshot(section)
    attempt.requirement_snapshot = snap
    return snap
