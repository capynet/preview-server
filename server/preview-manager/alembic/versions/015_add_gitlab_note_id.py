"""Add gitlab_note_id to previews for MR comment tracking.

Revision ID: 015
"""

revision = "015"
down_revision = "014"

from alembic import op


def upgrade():
    op.execute("ALTER TABLE previews ADD COLUMN IF NOT EXISTS gitlab_note_id BIGINT")
    # Fix: column may already exist as INTEGER, ensure it's BIGINT
    op.execute("ALTER TABLE previews ALTER COLUMN gitlab_note_id TYPE BIGINT")


def downgrade():
    op.execute("ALTER TABLE previews DROP COLUMN IF EXISTS gitlab_note_id")
