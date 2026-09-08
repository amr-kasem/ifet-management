"""Manual test capture — Impact, Forced Entry, ANSI Z97.1.

Revision ID: d1a6b93f2e57
Revises: c4e1f8a92b07
Create Date: 2026-09-08

Delivery plan §4.5, following the shape the deployed codebase already uses:

    static_tests  ──trials──> static_test_results (TestResult) ──> deflections
    cyclic_tests  ──trials──> cyclic_test_results (TestResult) ──> deflections
    manual_tests  ──trials──> manual_test_results (TestResult)
    missile_impact_tests ──trials──> impact_test_results (TestResult) ──> shots

**This replaces two earlier drafts** (`a3f7c21b9e04`, `b8d4e15a7c39`), which put
the attempt columns on the test row via a mixin. That duplicated `TestResult`
wholesale — `trial_number`, both UUIDs, the correction chain, the lifecycle and
the review columns are all already there, added by P1 — and it could not have
worked: a flat row cannot populate both `LabOS Test ID` and `LabOS Attempt ID`,
which the outbound envelope requires on every phase. Neither draft was ever
applied anywhere (production is at `3a65a83e0463`), so they are replaced rather
than corrected in a third migration.

Additive against live data. Verified read-only on the node 2026-09-08: 79
projects, 39 missile impact tests, 114 shots, with `missile`, `missile_weight`,
`shots.area`, `shots.velocity` and `shots.note` all `NOT NULL`. Nothing is
dropped; four columns are widened; one new column is backfilled before it is
made `NOT NULL`.
"""

import sqlalchemy as sa
from alembic import op

revision = "d1a6b93f2e57"
down_revision = "c4e1f8a92b07"
branch_labels = None
depends_on = None

_PROTOCOL_REF = [
    ("airtable_protocol_id", sa.String()),
    ("airtable_section_id", sa.String()),
    ("airtable_section_name", sa.String()),
]


def upgrade() -> None:
    # ---- the test row: Forced Entry + ANSI Z97.1, one table --------------
    #
    # One table with a `type` discriminator rather than two near-identical ones.
    # Both are a pass/fail outcome against a named grade or class, and
    # `static_tests` already carries a `type` column, so this is the pattern.
    op.create_table(
        "manual_tests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("required_option", sa.String(), nullable=True),
        sa.Column("finished", sa.Boolean(), nullable=False, server_default=sa.false()),
        *[sa.Column(n, t, nullable=True) for n, t in _PROTOCOL_REF],
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_manual_tests_id", "manual_tests", ["id"])
    op.create_index("ix_manual_tests_type", "manual_tests", ["type"])
    op.create_index("ix_manual_tests_airtable_section_id", "manual_tests",
                    ["airtable_section_id"])
    op.create_index("ix_manual_tests_airtable_protocol_id", "manual_tests",
                    ["airtable_protocol_id"])

    # ---- the attempt rows: joined-table subclasses of test_results -------
    #
    # Thin by design. Everything an attempt needs already lives on
    # `test_results`, so these carry only the link to their test — exactly as
    # `static_test_results` and `cyclic_test_results` do.
    for table, parent, fk in (
        ("manual_test_results", "manual_tests", "manual_test_id"),
        ("impact_test_results", "missile_impact_tests", "missile_impact_test_id"),
    ):
        op.create_table(
            table,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(fk, sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["id"], ["test_results.id"]),
            sa.ForeignKeyConstraint([fk], [f"{parent}.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(f"ix_{table}_id", table, ["id"])

    # ---- missile_impact_tests: Airtable linkage, and widened -------------
    #
    # 39 rows are already here. P1 gave `static_tests` and `cyclic_tests` these
    # three columns and missed Impact, which had no capture path at the time.
    for name, type_ in _PROTOCOL_REF:
        op.add_column("missile_impact_tests", sa.Column(name, type_, nullable=True))
    op.add_column("missile_impact_tests",
                  sa.Column("finished", sa.Boolean(), nullable=False,
                            server_default=sa.false()))
    op.create_index("ix_missile_impact_tests_airtable_section_id",
                    "missile_impact_tests", ["airtable_section_id"])
    op.create_index("ix_missile_impact_tests_airtable_protocol_id",
                    "missile_impact_tests", ["airtable_protocol_id"])
    op.alter_column("missile_impact_tests", "missile",
                    existing_type=sa.String(), nullable=True)
    op.alter_column("missile_impact_tests", "missile_weight",
                    existing_type=sa.Float(), nullable=True)

    # ---- shots: numbered, and attached to the attempt --------------------
    #
    # An impact test is a sequence — impact 1, 2, 3 — each with its own outcome
    # and its own photographs. "The third impact cracked the corner" is a
    # sentence someone has to be able to write, and a database id is not that
    # number.
    op.add_column("shots", sa.Column("shot_number", sa.Integer(), nullable=True))
    op.add_column("shots", sa.Column("test_result_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_shots_test_result_id", "shots", "test_results",
                          ["test_result_id"], ["id"])

    # Backfill by insertion order **within each test**. `id` is a sequence, so
    # for rows recorded through the old path it is the order they happened in —
    # the best evidence available, and better than leaving 114 rows unnumbered.
    op.execute("""
        UPDATE shots AS s SET shot_number = ordered.rn
        FROM (
            SELECT id, ROW_NUMBER() OVER (
                       PARTITION BY missile_impact_test_id ORDER BY id) AS rn
            FROM shots
        ) AS ordered
        WHERE s.id = ordered.id
    """)
    op.execute("UPDATE shots SET shot_number = 1 WHERE shot_number IS NULL")
    op.alter_column("shots", "shot_number",
                    existing_type=sa.Integer(), nullable=False)
    # Unique per ATTEMPT, not per test. Numbering restarts at 1 for each
    # attempt — that is the point of re-testing — so a constraint keyed on the
    # test would reject impact 1 of attempt 2. Legacy shots have a NULL
    # `test_result_id` and Postgres does not collide NULLs, so the 114 rows that
    # predate the attempt level are unaffected.
    op.create_unique_constraint("uq_shots_attempt_number", "shots",
                                ["test_result_id", "shot_number"])

    op.alter_column("shots", "area", existing_type=sa.Float(), nullable=True)
    op.alter_column("shots", "velocity", existing_type=sa.Float(), nullable=True)
    op.alter_column("shots", "note", existing_type=sa.String(), nullable=True)

    # ---- photographs, owned by the attempt -------------------------------
    #
    # One owner rather than one nullable FK per test type: the attempt is
    # already the common parent of all five types, so a photograph hangs off it
    # and inherits its identity. `shot_id` narrows it to a single impact.
    op.create_table(
        "test_photos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("test_result_id", sa.Integer(), nullable=False),
        sa.Column("shot_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["test_result_id"], ["test_results.id"]),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_test_photos_id", "test_photos", ["id"])
    op.create_index("ix_test_photos_shot_id", "test_photos", ["shot_id"])

    # ---- retest_required must be able to say "nobody has decided" --------
    #
    # P1 made this NOT NULL DEFAULT false, reasoning that "an omitted value must
    # not read as false". A non-nullable false does exactly that: it is
    # indistinguishable on the wire from a reviewer who considered a retest and
    # decided against one. Contract §6 is explicit — "meaningful only once
    # review exists, never inferred false from an unreviewed checkbox" — and
    # `mapping.py` already passes NULL through so the envelope omits the field.
    # Widening, so the 640 backfilled rows are untouched.
    op.alter_column("test_results", "retest_required",
                    existing_type=sa.Boolean(), nullable=True,
                    server_default=None)

    # ---- reviewer identity on the attempt --------------------------------
    #
    # The envelope has required `LabOS Verdict By` and `LabOS Verdict At` since
    # the fields were applied on 2026-09-06, and `mapping.py` reached for them
    # with getattr because nothing stored them. A verdict the builder accepts
    # but the database cannot keep is not a verdict.
    op.add_column("test_results", sa.Column("verdict_by", sa.String(), nullable=True))
    op.add_column("test_results",
                  sa.Column("verdict_at", sa.DateTime(timezone=True), nullable=True))

    # ---- requirement values that had nowhere to land ---------------------
    #
    # Both nullable, and that is the point: in the production Airtable base
    # `# Dials` and `Impact` are blank on most Protocol Sections, and a missing
    # requirement must never read as zero (write contract §5).
    op.add_column("projects", sa.Column("gauge_count", sa.Integer(), nullable=True))
    op.add_column("projects", sa.Column("impact_count", sa.Integer(), nullable=True))
    # Class 3 display metadata — Product Type, Height, Width, Service line.
    # Shown to the operator so it is not retyped; never executed from.
    op.add_column("projects", sa.Column("airtable_meta", sa.JSON(), nullable=True))

    # P1 linked `projects`; the job level was missed.
    op.add_column("project_parents",
                  sa.Column("airtable_project_id", sa.String(), nullable=True))
    op.create_index("ix_project_parents_airtable_project_id", "project_parents",
                    ["airtable_project_id"])


def downgrade() -> None:
    op.drop_index("ix_project_parents_airtable_project_id",
                  table_name="project_parents")
    op.drop_column("project_parents", "airtable_project_id")
    op.drop_column("projects", "airtable_meta")
    op.drop_column("projects", "impact_count")
    op.drop_column("projects", "gauge_count")

    # `retest_required` is deliberately NOT restored to NOT NULL: rows this
    # revision made legal carry NULL, and the ALTER would fail on them. Same
    # rule as the widenings below — a downgrade rolls back structure, not an
    # operator's data.
    op.drop_column("test_results", "verdict_at")
    op.drop_column("test_results", "verdict_by")

    op.drop_index("ix_test_photos_shot_id", table_name="test_photos")
    op.drop_index("ix_test_photos_id", table_name="test_photos")
    op.drop_table("test_photos")

    # The widenings are not reverted: restoring NOT NULL would fail against rows
    # this revision made legal. A downgrade rolls back structure, not an
    # operator's data.
    op.drop_constraint("uq_shots_attempt_number", "shots", type_="unique")
    op.drop_constraint("fk_shots_test_result_id", "shots", type_="foreignkey")
    op.drop_column("shots", "test_result_id")
    op.drop_column("shots", "shot_number")

    op.drop_index("ix_missile_impact_tests_airtable_protocol_id",
                  table_name="missile_impact_tests")
    op.drop_index("ix_missile_impact_tests_airtable_section_id",
                  table_name="missile_impact_tests")
    op.drop_column("missile_impact_tests", "finished")
    for name, _ in _PROTOCOL_REF:
        op.drop_column("missile_impact_tests", name)

    for table in ("impact_test_results", "manual_test_results"):
        op.drop_index(f"ix_{table}_id", table_name=table)
        op.drop_table(table)

    op.drop_index("ix_manual_tests_airtable_protocol_id", table_name="manual_tests")
    op.drop_index("ix_manual_tests_airtable_section_id", table_name="manual_tests")
    op.drop_index("ix_manual_tests_type", table_name="manual_tests")
    op.drop_index("ix_manual_tests_id", table_name="manual_tests")
    op.drop_table("manual_tests")
