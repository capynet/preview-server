"""Add cron_jobs column to projects and previews tables.

Revision ID: 017
Revises: 016
"""
from alembic import op

revision = "017"
down_revision = "016"


def upgrade():
    op.execute("""
        ALTER TABLE projects
        ADD COLUMN IF NOT EXISTS cron_jobs TEXT DEFAULT '[]'
    """)
    op.execute("""
        ALTER TABLE previews
        ADD COLUMN IF NOT EXISTS cron_jobs TEXT DEFAULT '[]'
    """)


def downgrade():
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS cron_jobs")
    op.execute("ALTER TABLE previews DROP COLUMN IF EXISTS cron_jobs")
