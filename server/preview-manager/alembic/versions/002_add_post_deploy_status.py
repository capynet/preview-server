"""Add post_deploy_status column to previews table.

Revision ID: 002
Revises: 001
"""
from alembic import op

revision = "002"
down_revision = "001"


def upgrade():
    op.execute("ALTER TABLE previews ADD COLUMN IF NOT EXISTS post_deploy_status TEXT")


def downgrade():
    op.execute("ALTER TABLE previews DROP COLUMN IF EXISTS post_deploy_status")
