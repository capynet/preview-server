"""Add phases column to deployments table.

Revision ID: 011
Revises: 010
"""
from alembic import op

revision = "011"
down_revision = "010"


def upgrade():
    op.execute("""
        ALTER TABLE deployments
        ADD COLUMN IF NOT EXISTS phases TEXT DEFAULT NULL
    """)


def downgrade():
    op.execute("ALTER TABLE deployments DROP COLUMN IF EXISTS phases")
