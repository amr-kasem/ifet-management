"""Per-artifact delivery tracking, and failed publication intent.

Revision ID: f7b2c04e19a5
Revises: e5f3a71c8d92
Create Date: 2026-09-08

Two tables, both for the same reason: a fact we were keeping somewhere that
could not express it.

**`sync_artifact_delivery`** — has *this photograph* reached Airtable?

`sync_attempt_state.delivered_seq` answers "what does Airtable hold for this
record", by sequence number, and both channels share one `attempt_seq` counter.
So delivering the verdict advanced the watermark past every earlier attachment,
and a parked photograph retried afterwards was classified superseded and
**silently discarded** — marked `done` having never been sent. Splitting the
queue heads fixed head-of-line blocking; it did not fix this.

Keyed on the photo, so both halves of contract §6 are expressible: a retry after
the verdict is still undelivered, and a redelivery of a photograph Airtable
already holds is a no-op rather than a second attachment.
`airtable_attachment_id` is what makes the second real — the id Airtable
returned, so an ambiguous upload is reconciled against the record instead of
guessed at.

**`sync_publication_failure`** — a payload we refused to queue.

`publish._refuse` wrote the reason onto the attempt (`airtable_sync_state`,
`airtable_sync_error`). Nothing read it: `state.status()` computes from the
queue and the worker only, so with no queue entry the headline read **Synced**
while the attempt was in fact never published — and `/sync/queue/{id}/retry`
operates on queue entries, which do not exist for these. A failure that is
invisible and unrepairable is worse than a parked entry.

This keeps the intent durably, **with the payload snapshot that was refused**,
so repair does not have to re-derive it from a database that has moved on.
"""

import sqlalchemy as sa
from alembic import op

revision = "f7b2c04e19a5"
down_revision = "e5f3a71c8d92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_artifact_delivery",
        sa.Column("photo_id", sa.Integer(), primary_key=True),
        sa.Column("attempt_id", sa.String(), nullable=False),
        sa.Column("airtable_record_id", sa.String(), nullable=True),
        # NULL until it lands. Present = delivered, and says which attachment.
        sa.Column("airtable_attachment_id", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("needs_reconciliation", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sync_artifact_delivery_attempt_id",
                    "sync_artifact_delivery", ["attempt_id"])

    op.create_table(
        "sync_publication_failure",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("attempt_id", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        # The values the envelope refused, kept verbatim. Repair must not have
        # to rebuild them from a row that has since changed.
        sa.Column("payload_snapshot", sa.JSON(), nullable=True),
        sa.Column("payload_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("recoverable", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        # One open failure per (attempt, phase): a second refusal of the same
        # phase updates the row rather than piling up duplicates a human then
        # has to read through.
        sa.UniqueConstraint("attempt_id", "phase",
                            name="uq_sync_publication_failure_attempt_phase"),
    )
    op.create_index("ix_sync_publication_failure_attempt_id",
                    "sync_publication_failure", ["attempt_id"])


def downgrade() -> None:
    op.drop_index("ix_sync_publication_failure_attempt_id",
                  table_name="sync_publication_failure")
    op.drop_table("sync_publication_failure")
    op.drop_index("ix_sync_artifact_delivery_attempt_id",
                  table_name="sync_artifact_delivery")
    op.drop_table("sync_artifact_delivery")
