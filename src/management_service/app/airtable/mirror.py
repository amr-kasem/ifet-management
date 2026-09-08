"""The local mirror of the Airtable hierarchy — read-only, and allowlisted.

**Why a mirror at all.** `report-api` must never call Airtable from a request
(delivery plan §4.6): an operator standing at a rig cannot have their picker
depend on someone else's API being up. So the hierarchy is copied locally and
every read serves from here. An empty mirror means an empty picker, not a
blocked operator.

**Why allowlisted, and what that means concretely.** The token is scoped per
*base*, so it can read all eight tables — customer emails, `Approved Proposal
Amount`, `Balance Due`, QuickBooks ids, wall reservations, back charges. Nothing
stops a well-meaning "just copy the record" from pulling all of it into our
database, and once it is here it is ours to leak. So the fields are named
explicitly, per table, below. A field not named is **not copied** — not ignored
downstream, not copied. That is the difference between a boundary and a
convention, and the change document tells the Airtable team the read boundary is
an application decision, so it had better be one we can point at.

**`Value` is deliberately absent from every allowlist.** Contract §10.19: it is
populated by a PDF extractor that drops blank cells, so a requirement of `+60/60`
can arrive as `9`, and every shifted value is individually plausible. LabOS reads
the typed fields instead. A future release must not "just add Value" — the
omission is the safety property.
"""

import datetime as dt

from sqlalchemy import (Boolean, Column, DateTime, Float, Integer, String,
                        Text, UniqueConstraint)

from ..data.models import Base

# --- the allowlists --------------------------------------------------------
#
# `{Airtable field name: local column}`. Exhaustive: the reader copies these and
# nothing else, and `assert_allowlisted` refuses a field outside them.

PROJECT_FIELDS = {
    "IFET job number": "job_number",
    "Project name": "project_name",
}

SPECIMEN_FIELDS = {
    "Mock-up/specimen name": "specimen_name",
    # **`IFET Job Number`, not `Project Name`.** The field register's IN row for
    # Mock-Ups/Specimens names `Project Name`, and that is not the link to
    # `IFET Projects` — it is a lookup of the job number's *text*. Read against
    # the live base on 2026-09-08: the record's link field is `IFET Job Number`
    # and carries `['reclD9DwtosvMGSI3']`.
    #
    # The register is a view, and the prose contract says the prose wins on
    # disagreement; here the *base* wins over both. Worth stating because a
    # register row that names a plausible neighbouring field is the kind of
    # error that survives review — the import simply found no specimens.
    "IFET Job Number": "project_record_id",  # a link field: [recId]
}

PROTOCOL_FIELDS = {
    "Protocol Name": "protocol_name",
    "Mock-Up": "specimen_record_id",         # link
}

SECTION_FIELDS = {
    "Section Name": "section_name",
    "Test Protocol": "protocol_record_id",   # link
    "Requirement Code": "requirement_code",
    "Requirement Kind": "requirement_kind",
    "Applicability": "applicability",
    "Required Value": "required_value",
    "Required Value Inward": "required_value_inward",
    "Required Value Outward": "required_value_outward",
    "Required Unit": "required_unit",
    "Required Option": "required_option",
    "Missile Type": "missile",
    "Missile Weight": "missile_weight",
    "Impact Velocity": "impact_velocity",
}

# Fields that exist and must never be read. Named so the refusal is explicit
# rather than incidental — a reader that simply "did not ask" would silently
# start copying them the day someone widened a query.
FORBIDDEN_FIELDS = frozenset({
    "Value",                    # §10.19 — the extractor's shifted column
    "Approved Proposal Amount", "Balance Due", "Billable Amount",
    "Internal Cost", "Approval Status", "Back Charge Name",
    "Customer Email", "QuickBooks ID",
})


def _now():
    return dt.datetime.now(dt.timezone.utc)


class MirrorRow:
    """Columns every mirror table carries."""

    record_id = Column(String, primary_key=True)
    # When we last read this row from Airtable. A stale mirror is a fact an
    # operator may need to see, so it is recorded rather than assumed fresh.
    mirrored_at = Column(DateTime(timezone=True), nullable=True)


class AtMirrorProject(Base, MirrorRow):
    __tablename__ = "at_mirror_projects"
    job_number = Column(String, nullable=True, index=True)
    project_name = Column(String, nullable=True)


class AtMirrorSpecimen(Base, MirrorRow):
    __tablename__ = "at_mirror_specimens"
    specimen_name = Column(String, nullable=True)
    project_record_id = Column(String, nullable=True, index=True)


class AtMirrorProtocol(Base, MirrorRow):
    __tablename__ = "at_mirror_protocols"
    protocol_name = Column(String, nullable=True)
    specimen_record_id = Column(String, nullable=True, index=True)


class AtMirrorSection(Base, MirrorRow):
    __tablename__ = "at_mirror_sections"
    section_name = Column(String, nullable=True)
    protocol_record_id = Column(String, nullable=True, index=True)
    requirement_code = Column(String, nullable=True, index=True)
    requirement_kind = Column(String, nullable=True)
    applicability = Column(String, nullable=True)
    required_value = Column(Float, nullable=True)
    required_value_inward = Column(Float, nullable=True)
    required_value_outward = Column(Float, nullable=True)
    required_unit = Column(String, nullable=True)
    required_option = Column(String, nullable=True)
    missile = Column(String, nullable=True)
    missile_weight = Column(Float, nullable=True)
    impact_velocity = Column(Float, nullable=True)


TABLES = (
    ("tblLYcRC7q6Srjfk3", AtMirrorProject, PROJECT_FIELDS),
    ("tblcrGv0WJn6FTTGO", AtMirrorSpecimen, SPECIMEN_FIELDS),
    ("tblutO1Q8TNC4BLk0", AtMirrorProtocol, PROTOCOL_FIELDS),
    ("tblqpvuJlSdkeS9PS", AtMirrorSection, SECTION_FIELDS),
)


class AllowlistViolation(Exception):
    """A field outside the allowlist reached the mirror. Never ignored."""


def assert_allowlisted(allowlist, fields):
    """Refuse anything the allowlist does not name. Raises, never filters.

    Filtering silently would make a widened query harmless *today* and a leak
    the day someone reads the filtered result and assumes it is complete. A
    forbidden field is named separately in the message, because copying
    `Balance Due` and copying `Value` are different mistakes: one is a privacy
    boundary, the other is the extractor defect (§10.19).
    """
    unknown = set(fields) - set(allowlist)
    if not unknown:
        return
    forbidden = sorted(unknown & FORBIDDEN_FIELDS)
    detail = f"fields outside the allowlist reached the mirror: {sorted(unknown)}"
    if forbidden:
        detail += (f" — and {forbidden} must NEVER be read: billing and "
                   "scheduling are outside the boundary, and `Value` carries "
                   "the extractor's shifted column (§10.19)")
    raise AllowlistViolation(detail)


def _first(value):
    """Airtable link fields arrive as a list of record ids. Take the one."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def upsert(session, model, allowlist, record, now=None):
    """Copy one Airtable record into the mirror, allowlisted fields only."""
    fields = record.get("fields") or {}
    assert_allowlisted(allowlist, fields)
    row = session.get(model, record["id"])
    if row is None:
        row = model(record_id=record["id"])
        session.add(row)
    for name, column in allowlist.items():
        if name in fields:
            setattr(row, column, _first(fields[name]))
    row.mirrored_at = now or _now()
    return row


def refresh(session, client, base_id=None, now=None):
    """Read the four hierarchy tables into the mirror. Returns per-table counts.

    **Requests only the allowlisted fields**, so a field outside them cannot
    arrive at all — `assert_allowlisted` is then a second line, catching the
    case where Airtable returns something we did not ask for.

    Idempotent: re-running updates rows in place and creates none. That is what
    makes a repeated import safe, and it is why the caller may run this as often
    as it likes.
    """
    counts = {}
    now = now or _now()
    for table_id, model, allowlist in TABLES:
        seen = 0
        offset = None
        while True:
            page = client.list_records(table_id, fields=list(allowlist),
                                       offset=offset)
            for record in page.get("records", []):
                upsert(session, model, allowlist, record, now=now)
                seen += 1
            offset = page.get("offset")
            if not offset:
                break
        counts[model.__tablename__] = seen
    return counts
