"""Add expose_config column to previews table.

Stores the expose service→port mapping from preview.yml so the
middleware can route exposed service domains to the correct port.

Revision ID: 005
Revises: 004
"""
from alembic import op

revision = "005"
down_revision = "004"


def upgrade():
    op.execute("ALTER TABLE previews ADD COLUMN IF NOT EXISTS expose_config TEXT DEFAULT '{}'")


def downgrade():
    op.execute("ALTER TABLE previews DROP COLUMN IF EXISTS expose_config")
