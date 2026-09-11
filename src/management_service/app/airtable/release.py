"""The requirement release gate — contract §3.3, DG14.

**What this exists to stop.** Their PDF extractor reads *values in order*
rather than *values by column*, so a blank cell slides everything after it one
place to the left and a 60 PSF requirement arrives as 9. LabOS has read the
typed design-pressure pair since 2026-09-08 and derives all fourteen static and
cyclic stages from it, so a shifted pair is a shifted test.

**And it cannot be detected.** 9 PSF is a number, in the right column, with the
right unit, of the right kind. Every shifted value is individually plausible —
that is the whole character of the defect — so there is no range check, no
sanity band and no "looks wrong" rule that would be anything but a new way to
be confidently incorrect. Nothing here tries.

What this does instead is what contract §3.3 already specifies, and it says to
do it here rather than in a screen:

    "Airtable requirements are displayed but cannot directly drive a rig. The
    operator must record the actual independently verified pair from the
    trusted proposal, its reference/revision, verifier and verification time.
    An `operator` provenance tag alone is insufficient. **Check this on the
    backend start path, not only in a UI.**"

So the control is **two independent readings of the same fact having to
agree**: what LabOS mirrored from Airtable, and what a named person read off
the trusted proposal. A shift makes them disagree, and a disagreement is
refused rather than resolved — LabOS does not choose a winner between two
sources that contradict each other, because picking one would mean publishing a
result claiming a requirement it may not have run against.

The chain, and nothing is executable until all of it holds:

    raw Airtable row
      -> mirror (allowlisted fields only)
      -> requirements.validate  — code, kind, unit, and the values the kind needs
      -> a typed inward/outward PSF pair
      -> independently verified against the proposal by a named person
      -> requirements.snapshot frozen onto the attempt at start
      -> executable

**A job with no Airtable origin is unaffected.** The operator typed its design
pressures in LabOS, as they always have; there is no second source to reconcile
and no extractor in the path. A9's promise holds for exactly that case, which
is what it was originally about.
"""

import datetime as dt

from . import requirements as req
from .mirror import AtMirrorSection

# Float-representation slack only. **This is not a plausibility band**: it
# exists because 60.0 read back from JSON and 60.0 typed by a person must
# compare equal, not because a pair that is nearly right is good enough. Two
# values that differ by more than this are two different requirements.
EPSILON = 1e-6

PAIR_CODES = ("STATIC_PRESSURE", "CYCLIC_PRESSURE")
PSF = "PSF"

# Reasons, as codes, so a UI can branch and a test can assert on something
# stabler than prose.
LOCAL = "local"
RELEASED = "released"
NO_SECTION = "no_section"
SECTION_MISSING = "section_missing"
NOT_A_PAIR = "not_a_pair"
INVALID = "invalid_requirement"
DRIFTED = "project_drifted_from_mirror"
UNVERIFIED = "unverified"
VERIFICATION_UNIT = "verification_unit"
DISAGREES = "verification_disagrees"


class ReleaseState:
    """Why this test may or may not run, in a form a screen can render.

    `executable` is the only thing the gate acts on. Everything else is here so
    the operator is told what to fix — a requirement that is merely refused,
    with no reason, is how an operator ends up re-importing at random.
    """

    __slots__ = ("executable", "code", "reason", "imported", "verified",
                 "section_id")

    def __init__(self, executable, code, reason="", imported=None,
                 verified=None, section_id=None):
        self.executable = executable
        self.code = code
        self.reason = reason
        self.imported = imported          # (inward, outward) from the mirror
        self.verified = verified          # (inward, outward) from the proposal
        self.section_id = section_id

    def as_dict(self):
        return {"executable": self.executable, "code": self.code,
                "reason": self.reason, "airtable_section_id": self.section_id,
                "imported_pair_psf": list(self.imported) if self.imported else None,
                "verified_pair_psf": list(self.verified) if self.verified else None}


def _pair(obj, inward_attr, outward_attr):
    a = getattr(obj, inward_attr, None)
    b = getattr(obj, outward_attr, None)
    if a is None or b is None:
        return None
    return (float(a), float(b))


def _same(a, b):
    return a is not None and b is not None and \
        abs(a[0] - b[0]) <= EPSILON and abs(a[1] - b[1]) <= EPSILON


def is_verified(project):
    """Has a named person recorded a verification against the proposal?

    All four facts, not just the numbers. §3.3 is explicit that a provenance
    tag alone is insufficient: a pair with no reference, no verifier and no
    time is an assertion nobody has signed.
    """
    return bool(
        project is not None
        and project.requirement_verified_inward is not None
        and project.requirement_verified_outward is not None
        and (project.requirement_reference or "").strip()
        and (project.requirement_verified_by or "").strip()
        and project.requirement_verified_at is not None)


def typed_pair(session, section_id):
    """The validated typed pair for a section, or a RequirementError.

    Re-validated here rather than trusted from import time, because the mirror
    is refreshed: a section edited upstream after the job was imported must be
    able to take the job back out of the executable state.
    """
    section = session.get(AtMirrorSection, section_id)
    if section is None:
        raise LookupError(section_id)
    req.validate(section)                          # raises RequirementError
    code = getattr(section, "requirement_code", None)
    if code not in PAIR_CODES:
        return None, section
    unit = getattr(section, "required_unit", None)
    if unit != PSF:
        # `validate` already refuses a unit the kind cannot mean. This is the
        # narrower statement the *rig* needs: fourteen stages are derived in
        # PSF, so anything else is not a unit conversion away from runnable.
        raise req.RequirementError(
            f"the design-pressure pair is in {unit!r}, and LabOS derives the "
            "static and cyclic stages in PSF. Refused rather than converted.")
    pair = _pair(section, "required_value_inward", "required_value_outward")
    if pair is None:
        raise req.RequirementError(
            "a Directional Pair needs both directions. Blank is never zero, "
            "and half a pair is not a requirement.")
    return pair, section


def evaluate(session, project, test):
    """May this static or cyclic test be released for execution?

    Read-only. Called by the gate below and served to the operator, so what a
    screen shows and what the backend enforces cannot drift apart.
    """
    section_id = getattr(test, "airtable_section_id", None)
    if not section_id:
        # **A LabOS-only job.** The operator entered these pressures here, as
        # they always have. There is no second source and no extractor.
        return ReleaseState(True, LOCAL,
                            "not an Airtable-imported test; the design "
                            "pressures are the operator's own input")

    try:
        pair, _section = typed_pair(session, section_id)
    except LookupError:
        return ReleaseState(
            False, SECTION_MISSING,
            f"the Airtable section {section_id} this test was imported from is "
            "no longer in the mirror, so its requirement cannot be confirmed. "
            "Refresh, and re-import if it has genuinely gone.",
            section_id=section_id)
    except req.RequirementError as exc:
        return ReleaseState(
            False, INVALID,
            f"the Airtable requirement is not readable unambiguously: {exc}",
            section_id=section_id)

    if pair is None:
        return ReleaseState(
            False, NOT_A_PAIR,
            "this test is bound to a section that carries no design-pressure "
            "pair, so there is nothing to derive its stages from.",
            section_id=section_id)

    stored = _pair(project, "inward_design_pressure", "outward_design_pressure")
    if not _same(pair, stored):
        # The upstream section changed after the job was imported. The project
        # would run stages derived from the old pair while claiming the new one.
        return ReleaseState(
            False, DRIFTED,
            f"the Airtable requirement now reads {list(pair)} PSF but this job "
            f"was imported as {list(stored) if stored else None} PSF. A test "
            "cannot be run against one requirement and reported against "
            "another — re-import, or restore the section.",
            imported=pair, section_id=section_id)

    if not is_verified(project):
        return ReleaseState(
            False, UNVERIFIED,
            "this requirement came from Airtable and has not been "
            "independently verified against the proposal. Contract §3.3: the "
            "pair, its reference, a named verifier and a time must be recorded "
            "before it can drive a rig, because the upstream extractor is "
            "known to shift values by one column and a shifted value is "
            "individually plausible. POST "
            "/projects/{id}/requirement-verification.",
            imported=pair, section_id=section_id)

    if project.requirement_verified_unit != PSF:
        return ReleaseState(
            False, VERIFICATION_UNIT,
            f"the recorded verification is in "
            f"{project.requirement_verified_unit!r}, not PSF.",
            imported=pair, section_id=section_id)

    verified = _pair(project, "requirement_verified_inward",
                     "requirement_verified_outward")
    if not _same(pair, verified):
        return ReleaseState(
            False, DISAGREES,
            f"the verified pair {list(verified)} PSF does not match what "
            f"Airtable now holds, {list(pair)} PSF. Two independent readings "
            "of the same requirement disagree, and LabOS does not choose "
            "between them: fix the section upstream, or re-verify against the "
            "proposal.",
            imported=pair, verified=verified, section_id=section_id)

    return ReleaseState(True, RELEASED, "verified against the proposal by "
                        f"{project.requirement_verified_by} "
                        f"({project.requirement_reference})",
                        imported=pair, verified=verified, section_id=section_id)


def verification_facts(project):
    """The verification, for freezing into the requirement snapshot.

    §3.3 requires the verification facts to be frozen with the rest of the
    evidence at run creation, not looked up later — by then the project may
    have been re-verified, and the attempt would report a verification that
    happened after it ran.
    """
    if not is_verified(project):
        return None
    at = project.requirement_verified_at
    return {
        "verified_inward_psf": float(project.requirement_verified_inward),
        "verified_outward_psf": float(project.requirement_verified_outward),
        "verified_unit": project.requirement_verified_unit,
        "reference": project.requirement_reference,
        "verified_by": project.requirement_verified_by,
        "verified_at": at.isoformat() if hasattr(at, "isoformat") else str(at),
    }


def record(project, *, inward, outward, unit, reference, verified_by,
           mirrored_pair, now=None):
    """Record a verification, or refuse it. Returns the reason it was refused.

    **A disagreement is refused rather than stored.** Storing it would leave
    the job permanently non-executable with a wrong number in it and no way
    back, and it would put a value in the record that nobody believes. The
    disagreement is real information and it belongs in front of the person who
    just produced it: either they misread the proposal, or the section is
    wrong upstream, and both are fixed by someone rather than by us.
    """
    if unit != PSF:
        return (f"the verified pair must be in PSF; {unit!r} was given. LabOS "
                "derives the stages in PSF and does not convert a requirement.")
    if not (reference or "").strip():
        return ("a proposal reference or revision is required. §3.3 is "
                "explicit that an operator tag alone is insufficient — the "
                "point is which document was read, not merely that one was.")
    if not (verified_by or "").strip():
        return "a named verifier is required."
    if mirrored_pair is None:
        return ("this job has no readable Airtable design-pressure pair to "
                "verify against.")
    if not _same((float(inward), float(outward)), mirrored_pair):
        return (f"the pair you verified, {[float(inward), float(outward)]} PSF, "
                f"does not match what LabOS mirrored from Airtable, "
                f"{list(mirrored_pair)} PSF. Nothing has been recorded. This is "
                "exactly the disagreement this check exists to surface: the "
                "upstream extractor is known to shift values by one column. "
                "Correct the Airtable section, or re-read the proposal — do "
                "not overwrite one with the other.")

    project.requirement_verified_inward = float(inward)
    project.requirement_verified_outward = float(outward)
    project.requirement_verified_unit = PSF
    project.requirement_reference = reference.strip()
    project.requirement_verified_by = verified_by.strip()
    project.requirement_verified_at = now or dt.datetime.now(dt.timezone.utc)
    return None
