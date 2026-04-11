"""Add deleted_at column to previews for soft-delete / resurrect.

Revision ID: 019
Revises: 018
"""
from alembic import op

revision = "019"
down_revision = "018"


def upgrade():
    op.execute("""
        ALTER TABLE previews
        ADD COLUMN IF NOT EXISTS deleted_at TEXT DEFAULT NULL
    """)
    # Partial index so active-preview queries (the vast majority) stay fast
    # even as soft-deleted rows accumulate.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_previews_active
        ON previews (project_id, preview_name)
        WHERE deleted_at IS NULL
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_previews_active")
    op.execute("ALTER TABLE previews DROP COLUMN IF EXISTS deleted_at")
