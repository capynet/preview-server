"""Add deleted_at column to users for anonymization-based deletion.

Revision ID: 021
Revises: 020
"""
from alembic import op

revision = "021"
down_revision = "020"


def upgrade():
    op.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS deleted_at TEXT DEFAULT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_active
        ON users (id)
        WHERE deleted_at IS NULL
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_users_active")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS deleted_at")
