"""Add target_branch column to previews table.

Stores the MR target branch so the UI can display it.

Revision ID: 006
Revises: 005
"""
from alembic import op

revision = "006"
down_revision = "005"


def upgrade():
    op.execute("ALTER TABLE previews ADD COLUMN IF NOT EXISTS target_branch TEXT")


def downgrade():
    op.execute("ALTER TABLE previews DROP COLUMN IF EXISTS target_branch")
