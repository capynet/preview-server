"""Add webhook_inbox table for durable at-least-once webhook processing.

Revision ID: 023
Revises: 022
"""
from alembic import op

revision = "023"
down_revision = "022"


def upgrade():
    # Timestamps are TEXT (ISO-8601 UTC strings), consistent with the rest of the
    # schema, which stores all *_at columns as sa.Text via the app's _now() helper.
    op.execute("""
        CREATE TABLE IF NOT EXISTS webhook_inbox (
            id           BIGSERIAL PRIMARY KEY,
            org_slug     TEXT NOT NULL,
            event        TEXT,
            gitlab_id    BIGINT,
            delivery_id  TEXT,
            payload      JSONB NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            attempts     INTEGER NOT NULL DEFAULT 0,
            error        TEXT,
            received_at  TEXT NOT NULL,
            processed_at TEXT
        )
    """)
    # Idempotency on GitLab's delivery UUID (partial: only when present).
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_webhook_delivery
        ON webhook_inbox (delivery_id) WHERE delivery_id IS NOT NULL
    """)
    # Fast lookup of work to drain.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_webhook_pending
        ON webhook_inbox (status) WHERE status IN ('pending', 'processing')
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS webhook_inbox")
