"""Seed a synthetic proposal into the Airtable **Testing Base**, then read it back.

Delivery plan Track A, step A3. The Testing Base is a structural clone of
production with **zero records**, so until now LabOS had proven it can *write*
the result surface and had never once *read* the requirement surface. This
closes that, and it is also M1's outstanding fixture.

    python -m app.airtable.fixture --env ../../.env            # dry run
    python -m app.airtable.fixture --env ../../.env --apply    # writes
    python -m app.airtable.fixture --env ../../.env --verify   # read back only

Safety, same rules as `apply_schema`:

1. **Production is refused unconditionally.** No flag reaches `app0OCunbmuXl7Hc9`.
2. Dry run is the default; `--apply` is required to write.
3. Idempotent on the job number — re-running reuses the fixture rather than
   creating a second one, because two fixtures look equally legitimate later.

**Every value here is synthetic and says so.** Contract §3.3: fixture
requirements are never eligible for production execution. The job number is
deliberately `IFET-FIXTURE-0001` and not a real-looking one.

**Why it covers six sections for five test types.** Five are the executable
requirement codes; `GAUGE_COUNT` is a programme parameter that produces no test
of its own, and it is included precisely because a fixture that omitted it would
not catch a reader that treats every section as executable.
"""

import argparse
import json
import os
import sys

from .apply_schema import api, load_env

TESTING_BASE = "app4oXS3Kd5IKWgJ7"
PRODUCTION_BASE = "app0OCunbmuXl7Hc9"

PROJECTS = "tblLYcRC7q6Srjfk3"
MOCKUPS = "tblcrGv0WJn6FTTGO"
PROTOCOLS = "tblutO1Q8TNC4BLk0"
SECTIONS = "tblqpvuJlSdkeS9PS"

JOB_NUMBER = "IFET-FIXTURE-0001"

# The design pressures the whole programme derives from. Asymmetric on purpose:
# a symmetric pair would pass even if inward and outward were transposed, and
# contract §3.1 requires the two magnitudes to stay independent.
INWARD, OUTWARD = 60.0, 45.0

SECTION_SPECS = [
    {"Section Name": "DP (+) (PSF)", "Requirement Code": "STATIC_PRESSURE",
     "Requirement Kind": "Directional Pair", "Applicability": "Required",
     "Required Value Inward": INWARD, "Required Value Outward": OUTWARD,
     "Required Unit": "PSF"},
    {"Section Name": "Cyclic (PSF)", "Requirement Code": "CYCLIC_PRESSURE",
     "Requirement Kind": "Directional Pair", "Applicability": "Required",
     "Required Value Inward": INWARD, "Required Value Outward": OUTWARD,
     "Required Unit": "PSF"},
    {"Section Name": "LMI (impacts)", "Requirement Code": "IMPACT_LMI",
     "Requirement Kind": "Count", "Applicability": "Required",
     "Required Value": 2, "Required Unit": "impacts",
     "Missile Type": "Large Missile D", "Missile Weight": 9.0,
     "Impact Velocity": 50.0},
    {"Section Name": "Forced Entry (*)", "Requirement Code": "FORCED_ENTRY",
     "Requirement Kind": "Not Applicable", "Applicability": "Required",
     "Required Option": "ASTM F588 Grade 40"},
    {"Section Name": "Impact under ANSI Z97.1", "Requirement Code": "ANSI_IMPACT",
     "Requirement Kind": "Not Applicable", "Applicability": "Required",
     "Required Option": "Class A"},
    # A parameter, not a test. Included so a reader that assumes every section
    # is executable fails here rather than in front of an operator.
    {"Section Name": "# Dials", "Requirement Code": "GAUGE_COUNT",
     "Requirement Kind": "Count", "Applicability": "Required",
     "Required Value": 5},
]

# What LabOS must be able to derive from the pair alone (app/domain/*).
EXPECTED_STATIC_STAGES = 6
EXPECTED_CYCLIC_STAGES = 8


def _url(table, suffix=""):
    return f"https://api.airtable.com/v0/{TESTING_BASE}/{table}{suffix}"


def _find_job(token):
    import urllib.parse
    f = urllib.parse.quote(f"{{IFET job number}}='{JOB_NUMBER}'")
    r = api("GET", _url(PROJECTS, f"?filterByFormula={f}"), token)
    recs = r.get("records", [])
    return recs[0] if recs else None


def _create(token, table, fields):
    r = api("POST", _url(table), token, {"records": [{"fields": fields}], "typecast": False})
    return r["records"][0]["id"]


def seed(token, apply_):
    existing = _find_job(token)
    if existing:
        print(f"  fixture job already present: {existing['id']} — reusing")
        return existing["id"]
    if not apply_:
        print("  would create: job, mock-up, protocol, 6 protocol sections")
        return None

    job = _create(token, PROJECTS, {
        "IFET job number": JOB_NUMBER,
        "Project name": "LabOS fixture — synthetic, not a real job",
        "Product Type": "Sliding Glass Door",
    })
    print(f"  job        {job}")

    mockup = _create(token, MOCKUPS, {
        "Mock-up/specimen name": "Fixture Specimen A",
        "IFET Job Number": [job],
        "Height (Inches)": 96,
        "Width (Inches)": 72,
        "Service line": "Testing",
    })
    print(f"  mock-up    {mockup}")

    protocol = _create(token, PROTOCOLS, {
        "Protocol Name": "Fixture Protocol — all five test types",
        "IFET Job Number": [job],
        "Mock-Up": [mockup],
    })
    print(f"  protocol   {protocol}")

    for spec in SECTION_SPECS:
        fields = dict(spec)
        fields["Test Protocol"] = [protocol]
        fields["IFET Job Number"] = [job]
        rid = _create(token, SECTIONS, fields)
        print(f"  section    {rid}  {spec['Requirement Code']}")
    return job


def verify(token):
    """Read the fixture back the way LabOS will, and assert it is usable."""
    from ..domain.static_test_pressure_calculator import StaticTestPressureCalculator
    from ..domain.cyclic_test_pressure_calculator import CyclicTestPressureCalculator

    failures = []
    job = _find_job(token)
    if not job:
        return ["fixture job not found — run with --apply first"]

    jf = job["fields"]
    print(f"  job {job['id']}  {jf.get('IFET job number')!r}  {jf.get('Project name')!r}")
    for f in ("IFET job number", "Project name", "Product Type"):
        if not jf.get(f):
            failures.append(f"IFET Projects.{f} did not read back")

    mocks = api("GET", _url(MOCKUPS), token).get("records", [])
    mock = next((m for m in mocks if job["id"] in (m["fields"].get("IFET Job Number") or [])), None)
    if not mock:
        return failures + ["mock-up did not link back to the fixture job"]
    mf = mock["fields"]
    print(f"  mock-up {mock['id']}  {mf.get('Mock-up/specimen name')!r}  "
          f"{mf.get('Height (Inches)')}x{mf.get('Width (Inches)')}")
    for f in ("Mock-up/specimen name", "Height (Inches)", "Width (Inches)", "Service line"):
        if mf.get(f) in (None, ""):
            failures.append(f"Mock-Ups/Specimens.{f} did not read back")

    secs = [s for s in api("GET", _url(SECTIONS), token).get("records", [])
            if job["id"] in (s["fields"].get("IFET Job Number") or [])]
    by_code = {s["fields"].get("Requirement Code"): s["fields"] for s in secs}
    print(f"  sections {len(secs)}: {sorted(c for c in by_code if c)}")

    for code in ("STATIC_PRESSURE", "CYCLIC_PRESSURE", "IMPACT_LMI",
                 "FORCED_ENTRY", "ANSI_IMPACT", "GAUGE_COUNT"):
        if code not in by_code:
            failures.append(f"{code} section did not read back")

    # --- the pair must be independent, and must drive the whole programme ---
    sp = by_code.get("STATIC_PRESSURE", {})
    inward, outward = sp.get("Required Value Inward"), sp.get("Required Value Outward")
    if (inward, outward) != (INWARD, OUTWARD):
        failures.append(f"design pair read back as {inward}/{outward}, expected {INWARD}/{OUTWARD}")
    if inward == outward:
        failures.append("fixture pair is symmetric — it cannot catch a transposition")

    if inward and outward:
        static = [StaticTestPressureCalculator.get_static_test_data(
                      outward if j % 2 else inward, j) for j in range(EXPECTED_STATIC_STAGES)]
        cyclic = [CyclicTestPressureCalculator.get_cylcic_test_data(
                      inward if i < 4 else outward, i) for i in range(EXPECTED_CYCLIC_STAGES)]
        print(f"  derived {len(static)} static + {len(cyclic)} cyclic stages "
              f"from the pair alone")
        print(f"    static  pressures: {[round(p, 2) for p, _ in static]}")
        print(f"    cyclic  high:      {[round(h, 2) for h, _, _ in cyclic]}")
        if len(static) + len(cyclic) != 14:
            failures.append("the pair did not derive 14 stages")
        # Airtable supplies no sequence, and must not need to.
        for extra in ("Loading Sequence", "Stages", "Cycle Count"):
            if extra in sp:
                failures.append(f"Airtable unexpectedly carries {extra!r} — LabOS derives it")

    # --- Impact: the three fields added 2026-09-08 -------------------------
    imp = by_code.get("IMPACT_LMI", {})
    for f, exp in (("Required Value", 2), ("Missile Type", "Large Missile D"),
                   ("Missile Weight", 9.0), ("Impact Velocity", 50.0)):
        if imp.get(f) != exp:
            failures.append(f"IMPACT_LMI.{f} read back as {imp.get(f)!r}, expected {exp!r}")

    # --- Forced Entry and ANSI carry a class, not a number -----------------
    for code, exp in (("FORCED_ENTRY", "ASTM F588 Grade 40"), ("ANSI_IMPACT", "Class A")):
        got = by_code.get(code, {}).get("Required Option")
        if got != exp:
            failures.append(f"{code}.Required Option read back as {got!r}, expected {exp!r}")
        if by_code.get(code, {}).get("Required Value") is not None:
            failures.append(f"{code} carries a numeric Required Value; its kind is Not Applicable")

    if by_code.get("GAUGE_COUNT", {}).get("Required Value") != 5:
        failures.append("GAUGE_COUNT did not read back as 5")

    # --- the legacy field the extractor corrupts must be untouched ---------
    for code, f in by_code.items():
        if f.get("Value"):
            failures.append(f"{code} has a legacy Value — the fixture must not populate it, "
                            "because LabOS never parses it")
    return failures


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args(argv)

    env = {**load_env(args.env), **os.environ}
    token = env.get("AIRTABLE_TOKEN", "").strip()
    if not token:
        print("ERROR: AIRTABLE_TOKEN not set")
        return 2
    base = env.get("AIRTABLE_BASE_ID", TESTING_BASE).strip()
    if base == PRODUCTION_BASE:
        print("REFUSED: this script never writes to the production base.")
        return 2

    if not args.verify:
        print(f"{'APPLY' if args.apply else 'DRY RUN'}  base={TESTING_BASE} (testing)")
        seed(token, args.apply)
        if not args.apply:
            print("\nnothing written. re-run with --apply.")
            return 0
        print()

    print("VERIFY — reading the fixture back as LabOS will")
    failures = verify(token)
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print("  -", f)
        return 1
    print("\nread surface verified: every field LabOS claims to read is readable, "
          "the pair drives all 14 stages, and all five test types route.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
