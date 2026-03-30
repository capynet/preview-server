"""Add magic_link_tokens table for passwordless email login.

Revision ID: 014
"""

revision = "014"
down_revision = "013"

from alembic import op


def upgrade():
    op.execute("""
        CREATE TABLE magic_link_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        )
    """)
    op.execute("CREATE INDEX idx_magic_link_token ON magic_link_tokens (token)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS magic_link_tokens")
