"""P1 — Airtable identity, append-only attempts, and the envelope fields

Revision ID: b7c2e9a41d38
Revises: 3a65a83e0463
Create Date: 2026-08-23

Week 2 / P1 of the LabOS <-> Airtable integration (Epic IFET-32, Refs 44-47 plus
internal-plan gap D). Implements write contract v0.3 §3, §4 and §6 in the
database.

`down_revision` is `3a65a83e0463` — the production head on 2026-08-23, read
from the live database (`SELECT * FROM alembic_version`). The 29 revisions
preceding this one were pulled off the node and committed the same day; before
that they were gitignored and existed on exactly one machine, so no migration
written anywhere else could resolve its parent. Of those 29, only
`886f54aa575c` ever created anything: the other 28 are empty no-ops, one per
container restart, produced by the `autogenerate` call that has now been removed
from `startup.sh`.

    !! BEFORE DEPLOYING, RE-CHECK THE HEAD !!

    Until the startup.sh change ships, every container restart still appends a
    no-op revision on the node and moves the head. If that happens after this
    file was written, the node will have a head this migration does not descend
    from, alembic will see TWO heads, and `upgrade head` fails with
    "Multiple head revisions are present" — the schema change silently does not
    apply.

    So: deploy the startup.sh change and this migration TOGETHER, and confirm
    immediately beforehand that

        SELECT * FROM alembic_version;

    still returns 3a65a83e0463. If it does not, pull the new revisions into
    the repo, re-point down_revision at the current head, and re-run
    tests/rehearse_p1_migration.py.

Three properties this migration is built around, because production has 623 live
attempt rows and there is currently no non-production node to rehearse on:

1.  **Purely additive.** New columns only. Nothing is dropped, renamed or
    retyped, so the running application keeps working against it untouched and
    a rollback is a column drop rather than a data-recovery exercise.

2.  **Every new column is nullable or defaulted**, so the ALTERs cannot fail on
    existing rows regardless of what those rows contain.

3.  **Pre-integration attempts are marked `Excluded`, not left blank.** This is
    the load-bearing line of the backfill. If historical rows were left with a
    NULL sync state and the W4 worker treated NULL as "not yet synced", the
    first run would upload every test IFET has ever performed into Airtable.
    Marking them explicitly means the default is "never send this", and only
    attempts created after the integration are eligible.
"""
import uuid

from alembic import op
import sqlalchemy as sa


revision = 'b7c2e9a41d38'
down_revision = '3a65a83e0463'
branch_labels = None
depends_on = None


# A fixed namespace so `labos_test_id` is derived deterministically rather than
# randomly: every existing attempt at the same test must end up with the SAME
# test id, or "attempt 2 of this test" becomes unexpressible for historical
# data. Re-running the backfill produces identical values.
_NS = uuid.UUID("5f2b1c94-3a7e-4d18-9c60-1e8a7d2f4b03")


# (column, type) for the attempt table — grouped as contract §4 groups them, so
# a reader can check this list against the contract section by section.
_ATTEMPT_COLUMNS = [
    # §4.1 identity
    ("labos_attempt_id", sa.String()),
    ("labos_test_id", sa.String()),
    ("schema_version", sa.String()),
    # §3.1 correction chain
    ("corrects_attempt_id", sa.String()),
    ("correction_reason", sa.Text()),
    # §4.3 lifecycle
    ("status", sa.String()),
    ("test_type", sa.String()),
    ("test_name", sa.String()),
    ("test_result", sa.String()),
    ("abort_reason", sa.String()),
    ("testing_continued", sa.String()),
    ("terminal_at", sa.DateTime(timezone=True)),
    # §4.4 measurements
    ("measured_value", sa.Float()),
    ("unit", sa.String()),
    ("max_pressure_achieved", sa.Float()),
    ("deflection_value", sa.Float()),
    ("deflection_unit", sa.String()),
    ("impact_result", sa.String()),
    ("cycles_required", sa.Integer()),
    ("cycles_completed", sa.Integer()),
    # what the attempt was tested AGAINST (contract §10.19 traceability)
    ("required_value", sa.Float()),
    ("required_unit", sa.String()),
    # §4.5 timing & people
    ("testing_start_date", sa.DateTime(timezone=True)),
    ("testing_end_date", sa.DateTime(timezone=True)),
    ("operator_name", sa.String()),
    # §4.6 artifacts & metadata
    ("photo_links", sa.Text()),
    ("report_link", sa.String()),
    ("excel_file_link", sa.String()),
    ("test_rig", sa.String()),
    ("labos_version", sa.String()),
    ("result_rationale", sa.Text()),
    ("labos_created_at", sa.DateTime(timezone=True)),
    ("labos_updated_at", sa.DateTime(timezone=True)),
    # gap D — the JSON columns
    ("result_detail", sa.JSON()),
    ("required_params", sa.JSON()),
    # sync visibility (the queue itself is W4 / Ref 55)
    ("airtable_sync_state", sa.String()),
    ("airtable_record_id", sa.String()),
    ("airtable_synced_at", sa.DateTime(timezone=True)),
    ("airtable_sync_error", sa.Text()),
]

_INDEXED = [
    ("ix_test_results_labos_test_id", "test_results", ["labos_test_id"]),
    ("ix_test_results_corrects_attempt_id", "test_results", ["corrects_attempt_id"]),
    ("ix_projects_airtable_project_id", "projects", ["airtable_project_id"]),
    ("ix_projects_airtable_mockup_id", "projects", ["airtable_mockup_id"]),
    ("ix_static_tests_airtable_protocol_id", "static_tests", ["airtable_protocol_id"]),
    ("ix_static_tests_airtable_section_id", "static_tests", ["airtable_section_id"]),
    ("ix_cyclic_tests_airtable_protocol_id", "cyclic_tests", ["airtable_protocol_id"]),
    ("ix_cyclic_tests_airtable_section_id", "cyclic_tests", ["airtable_section_id"]),
]


def upgrade() -> None:
    # ---- Airtable linkage (Refs 44/45) ---------------------------------
    op.add_column("projects", sa.Column("airtable_project_id", sa.String(), nullable=True))
    op.add_column("projects", sa.Column("airtable_mockup_id", sa.String(), nullable=True))
    op.add_column("projects", sa.Column("airtable_mockup_name", sa.String(), nullable=True))

    for table in ("static_tests", "cyclic_tests"):
        op.add_column(table, sa.Column("airtable_protocol_id", sa.String(), nullable=True))
        op.add_column(table, sa.Column("airtable_section_id", sa.String(), nullable=True))
        op.add_column(table, sa.Column("airtable_section_name", sa.String(), nullable=True))

    # ---- the attempt record (Refs 46/47 + gap D) ------------------------
    for name, type_ in _ATTEMPT_COLUMNS:
        op.add_column("test_results", sa.Column(name, type_, nullable=True))

    # `retest_required` is the one non-nullable addition: contract §4.5 says an
    # omitted value must NOT read as false, so the column carries an explicit
    # default rather than allowing NULL to stand in for "no".
    op.add_column(
        "test_results",
        sa.Column("retest_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    _backfill()

    # Unique index created AFTER the backfill, so it cannot collide with the
    # NULLs that existed a moment ago.
    op.create_index("ix_test_results_labos_attempt_id", "test_results",
                    ["labos_attempt_id"], unique=True)
    for name, table, cols in _INDEXED:
        op.create_index(name, table, cols)


def _backfill() -> None:
    """Give every pre-existing attempt an identity, and exclude it from sync."""
    bind = op.get_bind()

    # Which parent test each historical attempt belongs to. Attempts of the same
    # test must share a labos_test_id, and the only source of that grouping is
    # the subclass tables.
    for child_table, fk in (("static_test_results", "static_test_id"),
                            ("cyclic_test_results", "cyclic_test_id")):
        kind = child_table.split("_")[0]
        rows = bind.execute(sa.text(f"SELECT id, {fk} FROM {child_table}")).fetchall()
        for attempt_id, parent_id in rows:
            bind.execute(
                sa.text(
                    "UPDATE test_results SET labos_attempt_id = :aid, "
                    "labos_test_id = :tid WHERE id = :rid"
                ),
                {
                    "aid": str(uuid.uuid4()),
                    "tid": str(uuid.uuid5(_NS, f"{kind}:{parent_id}")),
                    "rid": attempt_id,
                },
            )

    # Any attempt not reachable through a subclass table (there should be none,
    # but the base table allows it) still needs a merge key to satisfy the
    # unique index.
    orphans = bind.execute(
        sa.text("SELECT id FROM test_results WHERE labos_attempt_id IS NULL")
    ).fetchall()
    for (attempt_id,) in orphans:
        bind.execute(
            sa.text("UPDATE test_results SET labos_attempt_id = :aid WHERE id = :rid"),
            {"aid": str(uuid.uuid4()), "rid": attempt_id},
        )

    # THE important line. See the module docstring: without this, the first run
    # of the W4 sync worker would upload every historical test into Airtable.
    # Lifecycle columns (status, test_result, ...) are deliberately left NULL —
    # these rows predate the contract and we do not retro-fit its semantics onto
    # them or claim things about them we cannot know.
    bind.execute(sa.text(
        "UPDATE test_results SET airtable_sync_state = 'Excluded' "
        "WHERE airtable_sync_state IS NULL"
    ))


def downgrade() -> None:
    op.drop_index("ix_test_results_labos_attempt_id", table_name="test_results")
    for name, table, _cols in _INDEXED:
        op.drop_index(name, table_name=table)

    op.drop_column("test_results", "retest_required")
    for name, _type in reversed(_ATTEMPT_COLUMNS):
        op.drop_column("test_results", name)

    for table in ("cyclic_tests", "static_tests"):
        op.drop_column(table, "airtable_section_name")
        op.drop_column(table, "airtable_section_id")
        op.drop_column(table, "airtable_protocol_id")

    op.drop_column("projects", "airtable_mockup_name")
    op.drop_column("projects", "airtable_mockup_id")
    op.drop_column("projects", "airtable_project_id")
