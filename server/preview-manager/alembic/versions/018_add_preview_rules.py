"""Add skip_source_branches and skip_target_branches to projects.

Revision ID: 018
Revises: 017
"""
from alembic import op

revision = "018"
down_revision = "017"


def upgrade():
    op.execute("""
        ALTER TABLE projects
        ADD COLUMN IF NOT EXISTS skip_source_branches TEXT DEFAULT '[]'
    """)
    op.execute("""
        ALTER TABLE projects
        ADD COLUMN IF NOT EXISTS skip_target_branches TEXT DEFAULT '[]'
    """)


def downgrade():
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS skip_source_branches")
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS skip_target_branches")
