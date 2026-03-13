"""Add composer_proxy_url to organizations.

Revision ID: 003
Revises: 002
"""
from alembic import op

revision = "003"
down_revision = "002"


def upgrade():
    op.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS composer_proxy_url TEXT NOT NULL DEFAULT ''")


def downgrade():
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS composer_proxy_url")
