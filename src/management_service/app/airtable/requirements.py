"""Reading a Protocol Section: what it requires, and refusing it when it cannot mean anything.

Contract §3.2 specifies this and the change document §0.2 asserts it to the
Airtable team — *"If a unit and a kind ever disagree, LabOS refuses the section
rather than assuming"*. It did not exist. The only artifact was a field
description inside Airtable, which is a note to a human, not a check.

**Why refusing matters more than it sounds.** A wrong unit is not a rounding
error, it is a different test: `60 in` where `60 PSF` was meant is not a
mis-typed pressure, it is a displacement requirement. And the failure mode this
guards is the one that already reached a Passed record — a value that is
individually plausible and wrong. So a section that cannot be read
unambiguously is *non-executable*, not best-guessed.

Reads the typed fields only. `Value` is never consulted; see `mirror`.
"""

# `Requirement Kind` -> the units that can mean anything for that kind.
# An empty set means the kind carries no unit at all.
UNITS_BY_KIND = {
    "Directional Pair": {"PSF"},
    "Magnitude": {"PSF", "in", "s"},
    "Count": {"impacts", "cycles", ""},
    "Enum": {""},
    "Not Applicable": {""},
}

# `Requirement Code` -> the kind it must carry. The code is the stable thing
# (names change, codes must not), so it is what routing keys on — and a code
# whose kind disagrees is a section built wrong, not a new case to support.
KIND_BY_CODE = {
    "STATIC_PRESSURE": "Directional Pair",
    "CYCLIC_PRESSURE": "Directional Pair",
    "WATER_PRESSURE": "Magnitude",
    "IMPACT_LMI": "Count",
    "IMPACT_SMI": "Count",
    "GAUGE_COUNT": "Count",
    "STATIC_PROGRAMME": "Enum",
    "FORCED_ENTRY": "Not Applicable",
    "ANSI_IMPACT": "Not Applicable",
}

# Codes LabOS can actually execute. `GAUGE_COUNT` and `STATIC_PROGRAMME` are
# parameters, not tests; `WATER_PRESSURE` is deferred by contract §0.
EXECUTABLE_CODES = frozenset({
    "STATIC_PRESSURE", "CYCLIC_PRESSURE", "IMPACT_LMI", "IMPACT_SMI",
    "FORCED_ENTRY", "ANSI_IMPACT",
})

REQUIRED = "Required"
NOT_REQUIRED = "Not Required"
UNCONFIRMED = "Unconfirmed"


class RequirementError(Exception):
    """The section cannot be read unambiguously. Not executable."""


def applicability_of(section):
    """`Required` / `Not Required` / `Unconfirmed`, with blank meaning the last.

    §2 of the change document: **blank means Unconfirmed, never Not Required.**
    Treating an empty cell as "not needed" would silently drop a test nobody had
    got round to marking, and the drop would look like a decision.
    """
    return getattr(section, "applicability", None) or UNCONFIRMED


def validate(section):
    """Raise `RequirementError` unless this section can be read unambiguously.

    Checks, in the order that puts the most structural problem first:

    1. the code is one we know — an unknown code routes nowhere;
    2. the kind matches the code, because the code is the stable identifier;
    3. the unit can mean something for that kind — the §3.2 check;
    4. the values present match the kind: a Directional Pair needs both
       directions, a Count or Magnitude needs its scalar, an Enum needs its
       option. **Blank is never zero** (§2), so a missing value is a refusal and
       not a default.
    """
    code = getattr(section, "requirement_code", None)
    if not code:
        raise RequirementError(
            "the section has no Requirement Code, so nothing can route on it. "
            "Names change and codes must not, which is why routing never uses "
            "Section Name.")
    if code not in KIND_BY_CODE:
        raise RequirementError(
            f"Requirement Code {code!r} is not one LabOS knows. Refused rather "
            f"than guessed: known codes are {sorted(KIND_BY_CODE)}.")

    kind = getattr(section, "requirement_kind", None)
    expected = KIND_BY_CODE[code]
    if kind and kind != expected:
        raise RequirementError(
            f"{code} must carry Requirement Kind {expected!r}, not {kind!r}. "
            "A kind that disagrees with its code is a section built wrong.")
    kind = kind or expected

    unit = (getattr(section, "required_unit", None) or "")
    allowed = UNITS_BY_KIND[kind]
    if unit not in allowed:
        raise RequirementError(
            f"Required Unit {unit!r} cannot mean anything for a {kind!r} "
            f"requirement (allowed: {sorted(allowed) or 'none'}). A wrong unit "
            "is not a rounding error — it is a different test, so the section "
            "is refused rather than assumed (contract §3.2).")

    inward = getattr(section, "required_value_inward", None)
    outward = getattr(section, "required_value_outward", None)
    value = getattr(section, "required_value", None)
    option = getattr(section, "required_option", None)

    if kind == "Directional Pair":
        missing = [n for n, v in (("Required Value Inward", inward),
                                  ("Required Value Outward", outward))
                   if v is None]
        if missing:
            raise RequirementError(
                f"{code} needs both directions; {missing} are blank. Blank is "
                "never zero — a missing design pressure is unknown, not 0 PSF.")
        if inward < 0 or outward < 0:
            raise RequirementError(
                f"design pressures are magnitudes and must be positive; got "
                f"inward={inward}, outward={outward}. Direction comes from the "
                "field name, never from a sign.")
    elif kind in ("Count", "Magnitude"):
        if value is None:
            raise RequirementError(
                f"{code} is a {kind} requirement and Required Value is blank. "
                "Blank is never zero.")
        if value < 0:
            raise RequirementError(f"{code} has a negative Required Value "
                                   f"({value}); magnitudes are positive.")
    elif kind == "Enum":
        if not option:
            raise RequirementError(
                f"{code} is an Enum requirement and Required Option is blank.")

    if kind == "Not Applicable" and value is not None:
        raise RequirementError(
            f"{code} is Not Applicable but carries Required Value {value!r}. A "
            "pass/fail test judged against a grade has no numeric requirement, "
            "so a number here means the section is describing something else.")
    return kind


def snapshot(section):
    """The requirement as a plain dict, for freezing onto an attempt.

    Only the typed fields, and only what was actually present — an absent value
    stays absent rather than becoming null-that-looks-like-zero downstream.
    """
    fields = ("requirement_code", "requirement_kind", "applicability",
              "required_value", "required_value_inward",
              "required_value_outward", "required_unit", "required_option",
              "missile", "missile_weight", "impact_velocity", "section_name")
    out = {"airtable_section_id": getattr(section, "record_id", None)}
    for name in fields:
        value = getattr(section, name, None)
        if value is not None:
            out[name] = value
    return out
