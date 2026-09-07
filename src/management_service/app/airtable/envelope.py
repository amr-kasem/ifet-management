"""Build the Airtable write payload — write contract v0.3 §4, §5, §5.1, §6.

Callers speak **LabOS vocabulary**; this module emits **wire names**. That
indirection is the whole point: the Airtable team renamed three of our fields
in guide v2 (`Airtable Mockup ID`, `LabOS Report Link`, `Complete LabOS JSON
Response`), and a rename should cost one line in `contract.py`, not a search
through the codebase.

Three rules here are worth stating plainly, because they are the ones that
protect the data rather than the code:

*   **Nothing is ever coerced.** An unmapped select option is a contract error
    surfaced as `Retry Required`, never quietly turned into free text (§5). A
    silent coercion produces a row that looks fine and is wrong.
*   **0 and False are data.** Only `None` means "no value", and "no value"
    means *omit the key* — never `""`, never `"N/A"` (§5).
*   **Fields the Airtable team has not created yet are not dropped.** They
    travel inside `Complete LabOS JSON Response` (§10.15) so no measurement is
    lost while the schema catches up. The two correction fields are the
    exception, and are handled explicitly below.
"""

import datetime as _dt
import json

from . import contract as C


class EnvelopeError(ValueError):
    """A contract violation, caught before anything is sent.

    Maps to `Retry Required` in the LabOS UI (§8): a human or a contract change
    has to fix it, so retrying the same payload is pointless.
    """


# --------------------------------------------------------------- value coercion

def _iso(value, field):
    """Normalise a datetime to ISO 8601 UTC with a trailing Z."""
    if isinstance(value, str):
        return value
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            # Naive datetimes are a silent-corruption risk: the reader cannot
            # tell UTC from local, and a lab in EDT is four hours out.
            raise EnvelopeError(
                f"{field!r}: naive datetime {value!r}; attach a timezone "
                "(contract §4.5 requires ISO 8601 UTC)"
            )
        return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, _dt.date):
        return value.isoformat()
    raise EnvelopeError(f"{field!r}: expected a datetime or ISO string, got {type(value).__name__}")


def _check_sentinel(name, value):
    if isinstance(value, str) and value.strip() in C.SENTINEL_VALUES:
        raise EnvelopeError(
            f"{name!r}: {value!r} is a sentinel, not a value. Contract §5 — "
            "omit the key instead (pass None)."
        )


def _check_option(field, value, live_options):
    """Validate a single-select value and return the spelling to send.

    Two checks with a translation between them, because LabOS and Airtable do
    not agree on how to spell several options (§10.16, §10.18):

    1.  `value` must be one of the LabOS options in contract §4 — this catches
        a caller inventing a value.
    2.  it is translated to the base's spelling (`Pass` -> `Passed`, and yes,
        `Aborted` -> `Abborted`, which is misspelt in their base).
    3.  the translated value must exist in the live option set, when the
        probe's snapshot has been passed in.

    Step 3 is what turns "their base has no option for this" into a loud local
    failure instead of a 422 — or, far worse, a silently created junk option if
    anyone ever sets `typecast`. `Test Type` is the live case: the base offers
    only `Static Load`, so four of the five LabOS test types cannot be written
    at all (§10.17).
    """
    if field.options and value not in field.options:
        raise EnvelopeError(
            f"{field.labos_name!r}: {value!r} is not an allowed option "
            f"{list(field.options)}. Contract §5 — LabOS never invents a select "
            "option at runtime; this is a contract error, not something to "
            "coerce."
        )

    wire_value = field.wire_option(value)

    if live_options and field.wire_name in live_options:
        allowed = live_options[field.wire_name]
        if allowed and wire_value not in allowed:
            hint = ""
            if wire_value != value:
                hint = f" (LabOS {value!r} translates to {wire_value!r})"
            raise EnvelopeError(
                f"{field.labos_name!r}: the live base has no option "
                f"{wire_value!r}{hint} — it offers {list(allowed)}. Contract §5: "
                "the option has to be added on the Airtable side; LabOS will not "
                "coerce or invent one."
            )
    return wire_value


# ------------------------------------------------------------------- the builder

def _resolve(values):
    """Validate keys and drop Nones. Returns {labos_name: value}."""
    unknown = [k for k in values if k not in C.BY_LABOS_NAME]
    if unknown:
        raise EnvelopeError(
            f"unknown field(s) {sorted(unknown)}: not in write contract §4. "
            "Add to contract.py (and the prose contract) before sending."
        )
    kept = {}
    for name, value in values.items():
        if value is None:
            continue           # §5: no value means omit the key
        _check_sentinel(name, value)
        kept[name] = value
    return kept


def _require(present, names, why):
    missing = [n for n in names if n not in present]
    if missing:
        raise EnvelopeError(f"missing required field(s) {missing} — {why}")


# Delivery phases (§4). `build()` needs to know which one it is building,
# because create, terminal and first review permit different fields and the
# same `Test Status` covers the last two.
CREATE = "create"
TERMINAL = "terminal"
VERDICT = "verdict"


def build(values, *, status, phase=None, live_options=None,
          allow_unreferenced_correction=False):
    """Build one Airtable record payload (wire names) for an upsert.

    `values` is keyed by LabOS canonical name (contract §4). Returns a plain
    dict suitable for `AirtableClient.upsert_records`.
    """
    if status not in (C.IN_PROGRESS,) + C.TERMINAL_STATUSES:
        raise EnvelopeError(f"unknown Test Status {status!r}")
    if phase is None:
        phase = CREATE if status == C.IN_PROGRESS else TERMINAL
    if phase not in (CREATE, TERMINAL, VERDICT):
        raise EnvelopeError(f"unknown phase {phase!r}")
    if phase == VERDICT and status == C.IN_PROGRESS:
        raise EnvelopeError(
            "a first review cannot be recorded against an In Progress attempt "
            "(contract §4 — evidence freezes on termination, then review)"
        )

    values = dict(values)
    values["Test Status"] = status
    values.setdefault("Schema Version", C.CONTRACT_VERSION)
    present = _resolve(values)

    test_type = present.get("Test Type")
    if test_type and test_type not in C.TEST_TYPES:
        raise EnvelopeError(f"unknown Test Type {test_type!r}; expected one of {list(C.TEST_TYPES)}")

    # ---- structural requirements ------------------------------------------
    _require(present, C.ALWAYS_REQUIRED, "contract §4.1 / §5 always-required set")

    if status == C.IN_PROGRESS:
        # Contract §6: "Creation explicitly sends Test Result = Pending." Until
        # 2026-09-07 this branch refused any Test Result at all, so the create
        # payload the contract mandates could not be built for any of the five
        # test types. What §4.3 actually forbids is a *verdict* before a review
        # has happened — Pending is the absence of a verdict, not one.
        #
        # Defaulted rather than merely permitted, because a blank cell and a
        # Pending cell read differently to a PM: blank says "nobody filled this
        # in", Pending says "running, awaiting review". Their automations see
        # the same difference.
        present.setdefault("Test Result", C.RESULT_PENDING)
        if present["Test Result"] != C.RESULT_PENDING:
            raise EnvelopeError(
                f"'Test Result' is {present['Test Result']!r} on an In Progress "
                f"write; only {C.RESULT_PENDING!r} is permitted before the first "
                "review (contract §4.3, §6)"
            )
    else:
        _require(present, C.TERMINAL_REQUIRED, "contract §4.5 terminal write")

        # §6: "Terminal result is Pending; first review updates verdict, Retest
        # Required, reviewer fields and JSON together." Delivery is three
        # phases, and the middle one is where the measurement lands, not the
        # judgement about it.
        #
        # Until 2026-09-07 the terminal builder demanded the final verdict
        # immediately, which collapsed those two phases into one. That is not a
        # cosmetic ordering point: it means a row can only be published once
        # somebody has already decided Pass or Fail, so either the result waits
        # in the queue until a reviewer gets to it, or the operator supplies a
        # verdict — and an operator-supplied verdict is exactly what §4's
        # separate reviewer identity exists to prevent.
        if phase == VERDICT:
            _require(present, C.VERDICT_REQUIRED, "contract §6 first-review write")
            if present["Test Result"] not in C.VERDICTS:
                raise EnvelopeError(
                    f"'Test Result' is {present['Test Result']!r} on a first-review "
                    f"write; expected one of {list(C.VERDICTS)} (contract §4)"
                )
        else:
            present.setdefault("Test Result", C.RESULT_PENDING)
            if present["Test Result"] != C.RESULT_PENDING:
                raise EnvelopeError(
                    f"'Test Result' is {present['Test Result']!r} on a terminal "
                    f"write; it stays {C.RESULT_PENDING!r} until the first review "
                    "records a verdict with a reviewer and a time against it "
                    "(contract §4, §6). Use build_verdict() for that phase."
                )
            for reviewer_only in ("LabOS Verdict By", "LabOS Verdict At",
                                  "Retest Required"):
                if reviewer_only in present:
                    raise EnvelopeError(
                        f"{reviewer_only!r} belongs to the first-review phase, not "
                        "the terminal write (contract §6). Retest Required in "
                        "particular is never inferred false from an unreviewed "
                        "checkbox."
                    )

    if status == C.COMPLETED:
        _require(present, C.REQUIRED_BY_TEST_TYPE.get(test_type, ()),
                 f"contract §5.1 required-by-test-type matrix for {test_type!r}")

    if status == C.ABORTED:
        _require(present, ("Abort Reason",),
                 "contract §4.3 — an Aborted row must record its cause")

    # ---- omitted-by-decision fields (A2, A3, A9) ---------------------------
    # Checked here, before the column/JSON split, because the contract rejects
    # these in **both** scalars and JSON. Routing an unvalidated measurement into
    # the overflow would satisfy the letter of "we do not publish it" while
    # publishing it, which is worse than sending it as a column: it would be
    # harder to notice.
    #
    # The point is not that the value is unavailable. It is that a value exists
    # and we cannot stand behind it - a target mistaken for an achievement, or
    # raw IO-Link counts mislabelled as inches.
    omitted = sorted(n for n in present
                     if n in C.BY_LABOS_NAME and C.BY_LABOS_NAME[n].omitted)
    if omitted:
        raise EnvelopeError(
            f"refusing to publish {omitted}: omitted by decision, not by "
            "absence. 'Max Pressure Achieved' has no measurement source at all "
            "(A2, milestone M7); deflection values are uncalibrated raw counts "
            "(A3, milestone M6); requirement echoes are not read from Airtable "
            "(A9). Rejected in the JSON as well as in the columns - a number we "
            "cannot stand behind is worse in the overflow than in a field, "
            "because nobody looks there."
        )

    # ---- pairwise rules ----------------------------------------------------
    if "Measured Value" in present:
        _require(present, ("Unit",), "contract §4.4 — Unit is required whenever Measured Value is sent")
    if "Deflection Value" in present:
        _require(present, ("Deflection Unit",),
                 "contract §4.4 — Deflection Unit is required whenever Deflection Value is sent")
    if "Correction Reason" in present and "Corrects Attempt ID" not in present:
        raise EnvelopeError("'Correction Reason' without 'Corrects Attempt ID' (contract §4.1)")

    # ---- the correction guard (§3.1, §10.14) -------------------------------
    if "Corrects Attempt ID" in present:
        _require(present, ("Correction Reason",),
                 "contract §4.1 — a correction must say why")
        field = C.BY_LABOS_NAME["Corrects Attempt ID"]
        if field.v2 == C.ABSENT and not allow_unreferenced_correction:
            raise EnvelopeError(
                "refusing to build a correction row: 'Corrects Attempt ID' does "
                "not exist in the Airtable base yet (contract §10.14). Without "
                "it Airtable cannot tell this correction from a genuine retest, "
                "so every roll-up that counts attempts or computes a pass rate "
                "would be wrong — and the row would look correct. Get the field "
                "added, or pass allow_unreferenced_correction=True to record it "
                "in the JSON field and Notes for a human to reconcile."
            )

    # ---- split: real columns vs. the JSON overflow -------------------------
    wire = {}
    overflow = {}
    detail = None

    for name, value in present.items():
        field = C.BY_LABOS_NAME[name]

        if name == "Result Detail (JSON)":
            detail = value
            continue
        # Both now have real dateTime columns (applied to the Testing Base
        # 2026-09-06), so they fall through and are written like any other
        # field. See the Test Date block below for what replaced §10.13.

        if field.kind in ("single select",) or (field.options and field.expected_live):
            value = _check_option(field, value, live_options)

        if field.kind in ("datetime", "date", "date/datetime"):
            value = _iso(value, name)

        if field.expected_live:
            wire[field.wire_name] = value
        else:
            # §10.15 — no column yet, so it rides in the JSON valve rather than
            # being silently dropped.
            overflow[_snake(name)] = value

    # ---- Test Date means completion (§5) -----------------------------------
    #
    # §10.13 used to collapse the start/end pair into their single `Test Date`,
    # because no start/end columns existed. Two of the fourteen fields applied
    # on 2026-09-06 were exactly those columns, and this block was not updated
    # with them: it went on skipping both real columns and stamping `Test Date`
    # with the **start** instant.
    #
    # That is worse than a missing field. `Test Date` held a real, plausible,
    # wrong timestamp — off by the duration of the test, with nothing on the row
    # to reveal it. Contract §5 is unambiguous: Test Date is execution
    # completion, omitted while running, and their automation derives the
    # Protocol Section date from it. Corrected 2026-09-07.
    started = present.get("Testing Start Date")
    ended = present.get("Testing End Date")
    if started is not None:
        overflow["testing_start_date"] = _iso(started, "Testing Start Date")
    if ended is not None:
        wire["Test Date"] = _iso(ended, "Testing End Date")
        overflow["testing_end_date"] = wire["Test Date"]
        if isinstance(started, _dt.datetime) and isinstance(ended, _dt.datetime):
            overflow["duration_s"] = int((ended - started).total_seconds())

    # ---- assemble the JSON valve (§6) --------------------------------------
    payload_detail = {}
    if isinstance(detail, dict):
        payload_detail.update(detail)
    elif isinstance(detail, str) and detail.strip():
        try:
            payload_detail.update(json.loads(detail))
        except ValueError:
            raise EnvelopeError("'Result Detail (JSON)' is a string but not valid JSON")
    payload_detail.setdefault("schema", C.CONTRACT_VERSION)
    if test_type:
        payload_detail.setdefault("test_type", test_type)
    if overflow:
        # Namespaced so it can never collide with a §6 test-type key.
        payload_detail["labos_extra"] = overflow

    if payload_detail:
        json_field = C.BY_LABOS_NAME["Result Detail (JSON)"].wire_name
        wire[json_field] = json.dumps(payload_detail, sort_keys=True, default=str)

    return wire


def _snake(name):
    out = []
    for ch in name:
        if ch.isalnum():
            out.append(ch.lower())
        elif out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_")


def build_start(values, **kw):
    """The `In Progress` write — partial payload, gives Airtable live visibility."""
    return build(values, status=C.IN_PROGRESS, **kw)


def build_terminal(values, *, status=C.COMPLETED, **kw):
    """The terminal write. Merges onto the start row via `LabOS Attempt ID`.

    Carries the measurement and freezes the evidence. `Test Result` stays
    `Pending` here — the verdict is a separate phase with its own reviewer.
    """
    if status not in C.TERMINAL_STATUSES:
        raise EnvelopeError(f"{status!r} is not a terminal status {list(C.TERMINAL_STATUSES)}")
    return build(values, status=status, phase=TERMINAL, **kw)


def build_verdict(values, *, status=C.COMPLETED, **kw):
    """The first-review write — verdict, reviewer, time and Retest Required.

    Recorded once (§4). It changes no measurement: everything else in the
    payload is the frozen identity the upsert merges on.
    """
    if status not in C.TERMINAL_STATUSES:
        raise EnvelopeError(f"{status!r} is not a terminal status {list(C.TERMINAL_STATUSES)}")
    return build(values, status=status, phase=VERDICT, **kw)


def options_from_snapshot(snapshot, table_id=None):
    """Extract live select options from a probe snapshot, for `live_options`.

    This is the link that keeps the builder honest: the probe reads what the
    base actually accepts, and the builder validates against that rather than
    against what the contract wishes were true.
    """
    from ..config import TABLE_RAW_DATA
    table = snapshot.get("tables", {}).get(table_id or TABLE_RAW_DATA, {})
    return {name: tuple(meta["options"])
            for name, meta in table.get("fields", {}).items()
            if meta.get("options")}


__all__ = ["build", "build_start", "build_terminal", "EnvelopeError",
           "build_verdict", "options_from_snapshot",
           "CREATE", "TERMINAL", "VERDICT"]
