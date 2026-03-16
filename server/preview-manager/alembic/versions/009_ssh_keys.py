"""Add ssh_keys table for user SSH key management.

Revision ID: 009
Revises: 008
"""
from alembic import op

revision = "009"
down_revision = "008"


def upgrade():
    op.execute("""
        CREATE TABLE ssh_keys (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            public_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS ssh_keys")
