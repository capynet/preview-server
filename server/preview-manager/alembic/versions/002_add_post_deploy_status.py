"""Add post_deploy_status to previews and type to deployments.

Revision ID: 002
Revises: 001
"""
from alembic import op

revision = "002"
down_revision = "001"


def upgrade():
    op.execute("ALTER TABLE previews ADD COLUMN IF NOT EXISTS post_deploy_status TEXT")
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS type TEXT NOT NULL DEFAULT 'deploy'")


def downgrade():
    op.execute("ALTER TABLE previews DROP COLUMN IF EXISTS post_deploy_status")
    op.execute("ALTER TABLE deployments DROP COLUMN IF EXISTS type")
