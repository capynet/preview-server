"""Add stack_info and domain_aliases columns to previews table.

Revision ID: 012
Revises: 011
"""
from alembic import op

revision = "012"
down_revision = "011"


def upgrade():
    op.execute("""
        ALTER TABLE previews
        ADD COLUMN IF NOT EXISTS stack_info TEXT DEFAULT NULL
    """)
    op.execute("""
        ALTER TABLE previews
        ADD COLUMN IF NOT EXISTS domain_aliases TEXT DEFAULT NULL
    """)


def downgrade():
    op.execute("ALTER TABLE previews DROP COLUMN IF EXISTS stack_info")
    op.execute("ALTER TABLE previews DROP COLUMN IF EXISTS domain_aliases")
