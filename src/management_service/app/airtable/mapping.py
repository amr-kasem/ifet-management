"""ORM attempt -> write-contract envelope (P1 / Ref 47).

This is the seam between the LabOS database and `envelope.build()`. It exists as
its own module, rather than as a method on the model, for one reason: the
mapping is a **statement about the contract**, and it should be reviewable
against contract §4 side by side without reading SQLAlchemy.

It deliberately does no validation. Every rule — which fields are required for
which test type, blank handling, option spelling, the JSON overflow — already
lives in `envelope.build()`, and duplicating any of it here would create a
second place for the contract to be wrong.

    values = envelope_values(attempt)
    payload = envelope.build(values, status=attempt.status, live_options=...)
"""

from . import contract as C
from .envelope import EnvelopeError

# Which ORM relationship leads from an attempt back to its test, per subclass.
# **All five test types, not two.** Every one of these tables carries the
# AirtableProtocolRef mixin, so once found they are interchangeable — the mixin
# is what makes the four Airtable IDs reachable from any attempt.
#
# `manual_test` and `missile_impact_test` were missing until 2026-09-08. The
# effect was not a partial payload: `envelope_values(strict=True)` raises when
# the linkage is absent, so **no Impact, Forced Entry or ANSI attempt could be
# published at all** — and the change document had already told the Airtable
# team that identity works for all five. A silent None here reads as "this
# attempt has no Airtable origin", which is a legitimate state for a
# locally-created job, so nothing distinguished the two.
_TEST_ATTRS = ("static_test", "cyclic_test", "manual_test", "missile_impact_test")


def owning_test(attempt):
    """The test this attempt belongs to, whichever of the five types it is.

    Returns None only when the attempt genuinely has no parent — which
    `envelope_values(strict=True)` then reports as missing linkage.
    """
    for attr in _TEST_ATTRS:
        test = getattr(attempt, attr, None)
        if test is not None:
            return test
    return None


def _impact_result(attempt):
    """The one-line Impact summary contract §5.1 requires on a terminal write.

    Derived from the numbered impacts rather than typed, because the operator
    already recorded each impact's outcome and asking twice invites the two to
    disagree. `attempt.impact_result` still wins if something set it.

    Free text by design — `Impact Result` is `singleLineText` in their base and
    the per-impact detail travels in the JSON, so this is the human-readable
    summary a person scanning the base sees. It names the failing impacts
    because "Fail" alone sends the reader to the JSON to learn which.

    None when there are no impacts, which correctly refuses the terminal
    payload: an Impact attempt cannot be completed without at least one impact,
    and the finish route enforces that before this is ever reached.
    """
    if attempt.test_type != C.IMPACT:
        return None
    shots = getattr(attempt, "shots", None) or []
    if not shots:
        return None
    total = len(shots)
    failed = sorted(sh.shot_number for sh in shots if sh.result is False)
    if failed:
        which = ", ".join(str(n) for n in failed)
        return f"Fail - impact {which} of {total} did not resist"
    unknown = [sh for sh in shots if sh.result is None]
    if unknown:
        # An impact with no recorded outcome is not a pass. Saying so is the
        # §4.5 rule that missing data is never a pass, applied to the summary.
        return f"Incomplete - {len(unknown)} of {total} impacts have no outcome"
    return f"Pass - {total} of {total} impacts resisted"


def _iso(value):
    return value.isoformat() if value is not None else None


def result_detail(attempt):
    """The §6 detailed JSON body — everything Airtable has no column for.

    **Built here, at enqueue time, and stored in the payload.** It is derived
    data, so a column would have to be kept in step with the row it describes;
    the outbox snapshots what the envelope produced and the worker never
    re-derives it, which is exactly the guarantee that makes deriving it here
    safe. `attempt.result_detail` still wins if something set it explicitly.

    §6 names the contents: identity, `attempt_kind`, execution start/end,
    requirements snapshot, procedure version, stage/observation detail, review
    and `data_quality`. This is also the only route to Airtable for the nine
    JSON-only fields of §10.15 — `Test Name`, `Abort Reason`, `Required Value`,
    `Required Unit`, `Cycles Required`, `Cycles Completed`, `Test Rig`,
    `LabOS Version` and `Result Rationale` — which have no scalar by decision.

    **Quarantined measurements appear as `data_quality` reasons and never as
    values** (§5: "send machine-readable data_quality reasons ... without
    exporting the untrusted numbers"). Deflections are raw IO-Link counts, so
    the JSON says a gauge was read and why the number is withheld. Publishing
    them here would satisfy the letter of "we do not send Deflection Value"
    while putting the same uncalibrated count somewhere nobody audits.
    """
    test = owning_test(attempt)
    project = getattr(test, "project", None) if test is not None else None

    detail = {
        "schema": C.CONTRACT_VERSION,
        "identity": {
            "labos_test_id": attempt.labos_test_id,
            "labos_attempt_id": attempt.labos_attempt_id,
            "attempt_number": attempt.trial_number,
            "airtable_project_id": getattr(project, "airtable_project_id", None),
            "airtable_mockup_id": getattr(project, "airtable_mockup_id", None),
            "airtable_protocol_id": getattr(test, "airtable_protocol_id", None),
            "airtable_section_id": getattr(test, "airtable_section_id", None),
            "identity_assurance": "declared",
        },
        # §4: a correction carries the original execution times and is not a
        # physical test; a retest is `execution` with new ones.
        "attempt_kind": "correction" if attempt.corrects_attempt_id else "execution",
        "execution": {
            "start": _iso(attempt.testing_start_date),
            "end": _iso(attempt.testing_end_date),
            "status": attempt.status,
            "testing_continued": attempt.testing_continued,
            "abort_reason": attempt.abort_reason,
        },
        "test": {
            "type": attempt.test_type,
            "name": attempt.test_name or getattr(test, "airtable_section_name", None),
            "rig": getattr(project, "device_id", None),
            "operator_name": attempt.operator_name,
        },
        "review": {
            "test_result": attempt.test_result,
            "verdict_by": attempt.verdict_by,
            "verdict_at": _iso(attempt.verdict_at),
            # Null until a review exists. Never inferred false (§6).
            "retest_required": attempt.retest_required,
            "rationale": getattr(attempt, "result_rationale", None),
        },
        "correction": {
            "corrects_attempt_id": attempt.corrects_attempt_id,
            "reason": attempt.correction_reason,
        } if attempt.corrects_attempt_id else None,
        "requirements": _requirements_snapshot(test, project),
        "observations": _observations(attempt),
        "data_quality": _data_quality(attempt),
    }
    return {k: v for k, v in detail.items() if v is not None}


def _requirements_snapshot(test, project):
    """What was required, as LabOS held it when the attempt ran.

    **Not a programme snapshot**, because `test_programmes` does not exist yet
    (business I/O reconciliation, 2026-09-08). This reads the live parent rows,
    which is correct for a test that has just run and would be wrong for one
    re-read years later — so it is marked `"source": "live"` rather than
    presented as frozen. Replacing it with a real snapshot is TC1.
    """
    if project is None and test is None:
        return None
    snapshot = {
        "source": "live",
        "inward_design_pressure_psf": getattr(project, "inward_design_pressure", None),
        "outward_design_pressure_psf": getattr(project, "outward_design_pressure", None),
        "gauge_count": getattr(project, "gauge_count", None),
        "impact_count": getattr(project, "impact_count", None),
        # Per-type requirements, present only on the type that has them.
        "required_option": getattr(test, "required_option", None),
        "missile": getattr(test, "missile", None),
        "missile_weight_lb": getattr(test, "missile_weight", None),
        "cycles_required": getattr(test, "cycles", None),
    }
    return {k: v for k, v in snapshot.items() if v is not None}


def _observations(attempt):
    """Numbered impacts, each with its own outcome and evidence count.

    Every numbered observation declares quantity, unit and source (§6). An
    unavailable value is omitted rather than sent as null or zero.
    """
    shots = getattr(attempt, "shots", None) or []
    if not shots:
        return None
    out = []
    for shot in sorted(shots, key=lambda x: (x.shot_number or 0)):
        item = {"shot_number": shot.shot_number, "result": shot.result,
                "photograph_count": len(shot.photos or [])}
        if shot.area is not None:
            item["area"] = {"quantity": shot.area, "unit": "in2",
                            "source": "operator"}
        if shot.velocity is not None:
            item["velocity"] = {"quantity": shot.velocity, "unit": "ft/s",
                                "source": "operator"}
        if shot.note:
            item["note"] = shot.note
        out.append(item)
    return out


def _data_quality(attempt):
    """Why a measurement is absent — machine-readable, without the number.

    One entry per withheld measurement, so a consumer can tell "not measured"
    from "measured and not trusted". That distinction is the entire reason this
    key exists: an empty `Deflection Value` alone cannot say which.
    """
    reasons = []
    deflections = getattr(attempt, "deflections", None) or []
    if deflections:
        reasons.append({
            "field": "Deflection Value",
            "reason": "uncalibrated_gauge_counts",
            "detail": (f"{len(deflections)} gauge(s) were read. The rigs return "
                       "raw IO-Link counts with no calibration to a physical "
                       "unit, so no deflection is published (A3). The counts are "
                       "retained in LabOS."),
        })
        reasons.append({
            "field": "recovery",
            "reason": "not_a_measurement",
            "detail": ("The rig's `recovery` value is the `recovery_time` "
                       "configuration constant, not an observation, so it is "
                       "never published as one."),
        })
    if attempt.test_type in (C.STATIC_LOAD, C.CYCLES):
        reasons.append({
            "field": "Max Pressure Achieved",
            "reason": "not_persisted",
            "detail": ("Actual pressure is present on the rig's telemetry bus "
                       "but nothing subscribes to it and stores the maximum, so "
                       "there is no value to publish."),
        })
        reasons.append({
            "field": "Measured Value",
            "reason": "no_source",
            "detail": ("The rig reports no measurement for this test type; the "
                       "setpoint is a target, not an achievement (A2)."),
        })
    return reasons or None


def _deflection_value(attempt):
    """Largest gauge reading, unless an explicit column overrides it.

    **Currently unused by `envelope_values`** — A3 quarantines deflection, so
    nothing publishes this. Kept, with its tests, because milestone M6
    un-quarantines it once a known displacement has been applied to a gauge and
    the transform identified end to end. Deleting it would mean rediscovering
    the derivation later; leaving it wired would mean publishing raw IO-Link
    counts mislabelled as inches.

    Prefer the explicit column: an operator or the pass/fail step may have
    chosen which gauge is authoritative. Fall back to the largest measured
    deflection across gauges, which is the conservative reading and the one a
    structural pass/fail turns on.
    """
    if attempt.deflection_value is not None:
        return attempt.deflection_value
    readings = [d.max_deflection for d in (attempt.deflections or [])
                if d.max_deflection is not None]
    return max(readings) if readings else None


def envelope_values(attempt, *, strict=True):
    """Build the `{LabOS field name: value}` dict for `envelope.build()`.

    `strict` raises when the Airtable linkage is missing. That linkage is what
    tells Airtable which Protocol Section a result belongs to, so a payload
    without it is not a partially-good record — it is a record that would land
    unattached to anything. Pass `strict=False` only to inspect a draft.
    """
    test = owning_test(attempt)
    project = getattr(test, "project", None) if test is not None else None

    if strict:
        missing = []
        if project is None or not project.airtable_project_id:
            missing.append("Airtable Project ID")
        if project is None or not project.airtable_mockup_id:
            missing.append("Airtable Mock-Up ID")
        if test is None or not test.airtable_protocol_id:
            missing.append("Airtable Protocol ID")
        if test is None or not test.airtable_section_id:
            missing.append("Airtable Section ID")
        if missing:
            raise EnvelopeError(
                f"attempt {attempt.labos_attempt_id!r} has no Airtable linkage for "
                f"{missing} (contract §4.1). The project or protocol has not been "
                "bound to an Airtable record yet — bind it, or exclude this "
                "attempt from sync. A row written without linkage attaches to "
                "nothing on their side."
            )

    values = {
        # -- §4.1 identity ---------------------------------------------------
        "Airtable Project ID": getattr(project, "airtable_project_id", None),
        "Airtable Mock-Up ID": getattr(project, "airtable_mockup_id", None),
        "Airtable Protocol ID": getattr(test, "airtable_protocol_id", None),
        "Airtable Section ID": getattr(test, "airtable_section_id", None),
        "LabOS Test ID": attempt.labos_test_id,
        "LabOS Attempt ID": attempt.labos_attempt_id,
        "Attempt Number": attempt.trial_number,
        "Schema Version": attempt.schema_version or C.CONTRACT_VERSION,
        "Corrects Attempt ID": attempt.corrects_attempt_id,
        "Correction Reason": attempt.correction_reason,

        # -- §4.3 descriptors -------------------------------------------------
        # Test Name falls back to the Airtable Section Name, which is what a
        # human reading their base would recognise the row by.
        "Test Name": attempt.test_name or getattr(test, "airtable_section_name", None),
        "Test Type": attempt.test_type,
        "Test Result": attempt.test_result,
        "Abort Reason": attempt.abort_reason,

        # -- §4.4 measurements -------------------------------------------------
        #
        # Five columns are deliberately NOT emitted here, and their absence is
        # the decision rather than an oversight:
        #
        #   Max Pressure Achieved  A2 - no measurement source exists at all. The
        #                          rig sends deflections[] and nothing else, and
        #                          the setpoint is a target. M7.
        #   Deflection Value/Unit  A3 - raw IO-Link counts mislabelled as inches.
        #                          Uncalibrated evidence stays local. M6.
        #
        # `Required Value` and `Required Unit` are NOT in that list and are still
        # emitted below: A9 stops LabOS reading requirement values from Airtable,
        # not publishing the ones its own operator entered. Those are LabOS's
        # numbers and did not come through the extractor.
        #
        # They were emitted until 2026-09-06. The envelope now refuses them in
        # both the columns and the JSON, so emitting them here would raise rather
        # than publish - but the honest fix is not to produce them in the first
        # place. The ORM columns stay: the data is kept locally, it is only
        # publishing it that is refused.
        "Measured Value": attempt.measured_value,
        "Unit": attempt.unit,
        "Impact Result": attempt.impact_result or _impact_result(attempt),
        "Required Value": attempt.required_value,
        "Required Unit": attempt.required_unit,
        "Cycles Completed": attempt.cycles_completed,
        "Result Detail (JSON)": attempt.result_detail or result_detail(attempt),

        # -- §4.5 timing, people, disposition ---------------------------------
        "Testing Start Date": attempt.testing_start_date,
        "Testing End Date": attempt.testing_end_date,
        "Operator Name": attempt.operator_name,
        # §6: "Retest Required is meaningful only once review exists, never
        # inferred false from an unreviewed checkbox."
        #
        # This was `bool(attempt.retest_required)` until 2026-09-07, which did
        # precisely what that sentence forbids: an unreviewed attempt has None
        # here, and bool(None) is False — an unearned answer, indistinguishable
        # on the wire from a reviewer who considered a retest and decided
        # against one. Passed through untouched now, so an unreviewed attempt
        # omits the key and the envelope's phase guard keeps it out of the
        # terminal write entirely.
        "Retest Required": (None if attempt.retest_required is None
                            else bool(attempt.retest_required)),
        # Written once by the first review, with the verdict (§6). getattr
        # because the reviewer columns do not exist on the legacy attempt model
        # yet — that persistence is the other half of step 3.
        "LabOS Verdict By": getattr(attempt, "verdict_by", None),
        "LabOS Verdict At": getattr(attempt, "verdict_at", None),
        "Testing Continued": attempt.testing_continued,
        "Notes": attempt.note,

        # -- §4.6 artifacts & metadata -----------------------------------------
        "Photos": attempt.photo_links,
        "Excel File Link": attempt.excel_file_link,
        "Report Link": attempt.report_link,
        "LabOS Created At": attempt.labos_created_at,
        "LabOS Updated At": attempt.labos_updated_at,
        "Test Rig": attempt.test_rig,
        "LabOS Version": attempt.labos_version,
        "Result Rationale": attempt.result_rationale,
    }

    # §5: a key with no value is omitted entirely rather than sent blank. The
    # envelope enforces this too; dropping them here keeps the payload readable
    # in logs and makes the two layers agree rather than merely not conflict.
    return {k: v for k, v in values.items() if v is not None}


def is_syncable(attempt):
    """Whether the sync worker (W4) may write this attempt at all.

    Two independent reasons to refuse, both contract-level rather than
    operational:

    * `Excluded` — pre-integration attempts, marked by the P1 migration. Without
      this the first worker run would upload every test IFET has ever done.
    * terminal + already synced — contract §3 makes a terminal attempt final.
      Once written, LabOS never writes that attempt ID again; a correction is a
      new row, not an edit.
    """
    if attempt.airtable_sync_state == "Excluded":
        return False
    if attempt.terminal_at is not None and attempt.airtable_synced_at is not None:
        return False
    return True
