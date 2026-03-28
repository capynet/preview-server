"""Add public_paths column to projects table.

Revision ID: 013
Revises: 012
"""
from alembic import op

revision = "013"
down_revision = "012"


def upgrade():
    op.execute("""
        ALTER TABLE projects
        ADD COLUMN IF NOT EXISTS public_paths TEXT DEFAULT NULL
    """)


def downgrade():
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS public_paths")
