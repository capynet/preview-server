"""Replace composer_proxy_url with composer_proxy_enabled.

Revision ID: 004
Revises: 003
"""
from alembic import op

revision = "004"
down_revision = "003"


def upgrade():
    op.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS composer_proxy_enabled INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS composer_proxy_url")


def downgrade():
    op.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS composer_proxy_url TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS composer_proxy_enabled")
