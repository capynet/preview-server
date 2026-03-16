"""Add require_ci_success to organizations/projects and ci_status to previews.

Revision ID: 010
Revises: 009
"""
from alembic import op

revision = "010"
down_revision = "009"


def upgrade():
    op.execute("""
        ALTER TABLE organizations
        ADD COLUMN IF NOT EXISTS require_ci_success INTEGER NOT NULL DEFAULT 0
    """)
    op.execute("""
        ALTER TABLE projects
        ADD COLUMN IF NOT EXISTS require_ci_success INTEGER DEFAULT NULL
    """)
    op.execute("""
        ALTER TABLE previews
        ADD COLUMN IF NOT EXISTS ci_status TEXT DEFAULT NULL
    """)


def downgrade():
    op.execute("ALTER TABLE previews DROP COLUMN IF EXISTS ci_status")
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS require_ci_success")
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS require_ci_success")
