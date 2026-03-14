"""Membership redesign: add role to project_members, drop password_hash.

Revision ID: 007
Revises: 006
"""
from alembic import op

revision = "007"
down_revision = "006"


def upgrade():
    # Add role column to project_members
    op.execute(
        "ALTER TABLE project_members ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'viewer'"
    )
    # Drop password_hash from users (OAuth only)
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS password_hash")


def downgrade():
    op.execute("ALTER TABLE project_members DROP COLUMN IF EXISTS role")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
