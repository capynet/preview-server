"""Add system_role to users table.

Revision ID: 016
"""
from alembic import op
import sqlalchemy as sa

revision = "016"
down_revision = "015"


def upgrade():
    op.add_column("users", sa.Column("system_role", sa.String(50), nullable=True))


def downgrade():
    op.drop_column("users", "system_role")
