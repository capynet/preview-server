"""Allow same SSH fingerprint across users; enforce UNIQUE per (user_id, fingerprint).

Revision ID: 020
Revises: 019
"""
from alembic import op

revision = "020"
down_revision = "019"


def upgrade():
    op.execute("ALTER TABLE ssh_keys DROP CONSTRAINT IF EXISTS ssh_keys_fingerprint_key")
    op.execute("ALTER TABLE ssh_keys ADD CONSTRAINT ssh_keys_user_fingerprint_key UNIQUE (user_id, fingerprint)")


def downgrade():
    op.execute("ALTER TABLE ssh_keys DROP CONSTRAINT IF EXISTS ssh_keys_user_fingerprint_key")
    op.execute("ALTER TABLE ssh_keys ADD CONSTRAINT ssh_keys_fingerprint_key UNIQUE (fingerprint)")
