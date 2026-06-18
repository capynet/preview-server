"""Add gitlab_token_expires_at to organizations for token auto-rotation.

Revision ID: 022
Revises: 021
"""
from alembic import op

revision = "022"
down_revision = "021"


def upgrade():
    op.execute("""
        ALTER TABLE organizations
        ADD COLUMN IF NOT EXISTS gitlab_token_expires_at TEXT DEFAULT NULL
    """)


def downgrade():
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS gitlab_token_expires_at")
