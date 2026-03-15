"""Make api_tokens.organization_id nullable for user-scoped CLI tokens.

Revision ID: 008
Revises: 007
"""
from alembic import op

revision = "008"
down_revision = "007"


def upgrade():
    # Allow CLI tokens without org scope
    op.execute(
        "ALTER TABLE api_tokens ALTER COLUMN organization_id DROP NOT NULL"
    )


def downgrade():
    # Set any NULL org tokens to first available org before re-adding constraint
    op.execute(
        "ALTER TABLE api_tokens ALTER COLUMN organization_id SET NOT NULL"
    )
