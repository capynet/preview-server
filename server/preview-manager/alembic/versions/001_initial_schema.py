"""Initial schema — all tables.

Revision ID: 001
Revises: None
Create Date: 2026-03-11
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('email', sa.Text, unique=True, nullable=False),
        sa.Column('name', sa.Text, nullable=False, server_default=''),
        sa.Column('avatar_url', sa.Text),
        sa.Column('password_hash', sa.Text),
        sa.Column('is_superadmin', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created_at', sa.Text, nullable=False),
        sa.Column('updated_at', sa.Text, nullable=False),
    )

    op.create_table(
        'oauth_accounts',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('provider', sa.Text, nullable=False),
        sa.Column('provider_user_id', sa.Text, nullable=False),
        sa.Column('provider_username', sa.Text),
        sa.Column('created_at', sa.Text, nullable=False),
        sa.UniqueConstraint('provider', 'provider_user_id'),
    )

    op.create_table(
        'sessions',
        sa.Column('id', sa.Text, primary_key=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.Text, nullable=False),
        sa.Column('expires_at', sa.Text, nullable=False),
    )

    op.create_table(
        'organizations',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('slug', sa.Text, unique=True, nullable=False),
        sa.Column('name', sa.Text, nullable=False),
        sa.Column('avatar_url', sa.Text),
        sa.Column('gitlab_url', sa.Text),
        sa.Column('gitlab_access_token', sa.Text),
        sa.Column('auto_erase_enabled', sa.Integer, nullable=False, server_default='0'),
        sa.Column('auto_erase_days', sa.Integer, nullable=False, server_default='10'),
        sa.Column('color', sa.Text, nullable=False, server_default="'#6366f1'"),
        sa.Column('created_at', sa.Text, nullable=False),
        sa.Column('updated_at', sa.Text, nullable=False),
    )

    op.create_table(
        'org_members',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('organization_id', sa.Integer, sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('role', sa.Text, nullable=False),
        sa.Column('created_at', sa.Text, nullable=False),
        sa.UniqueConstraint('user_id', 'organization_id'),
    )

    op.create_table(
        'org_email_domains',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('organization_id', sa.Integer, sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('domain', sa.Text, unique=True, nullable=False),
        sa.Column('default_role', sa.Text, nullable=False, server_default="'member'"),
        sa.Column('created_at', sa.Text, nullable=False),
    )

    op.create_table(
        'projects',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('organization_id', sa.Integer, sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('slug', sa.Text, nullable=False),
        sa.Column('name', sa.Text),
        sa.Column('gitlab_project_id', sa.Integer),
        sa.Column('gitlab_project_path', sa.Text),
        sa.Column('gitlab_web_url', sa.Text),
        sa.Column('gitlab_default_branch', sa.Text, server_default="'main'"),
        sa.Column('env_vars', sa.Text, server_default="'{}'"),
        sa.Column('created_at', sa.Text, nullable=False),
        sa.Column('updated_at', sa.Text, nullable=False),
        sa.UniqueConstraint('organization_id', 'slug'),
    )

    op.create_table(
        'org_invitations',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('organization_id', sa.Integer, sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('email', sa.Text, nullable=False),
        sa.Column('role', sa.Text, nullable=False),
        sa.Column('token', sa.Text, unique=True, nullable=False),
        sa.Column('project_id', sa.Integer, sa.ForeignKey('projects.id', ondelete='SET NULL')),
        sa.Column('invited_by', sa.Integer, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('status', sa.Text, nullable=False, server_default="'pending'"),
        sa.Column('created_at', sa.Text, nullable=False),
        sa.Column('expires_at', sa.Text, nullable=False),
    )

    op.create_table(
        'api_tokens',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('organization_id', sa.Integer, sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.Text, nullable=False, server_default="''"),
        sa.Column('token_hash', sa.Text, unique=True, nullable=False),
        sa.Column('token_prefix', sa.Text, nullable=False),
        sa.Column('created_at', sa.Text, nullable=False),
        sa.Column('last_used_at', sa.Text),
    )

    op.create_table(
        'cli_auth_requests',
        sa.Column('code', sa.Text, primary_key=True),
        sa.Column('organization_id', sa.Integer, sa.ForeignKey('organizations.id')),
        sa.Column('status', sa.Text, nullable=False, server_default="'pending'"),
        sa.Column('user_id', sa.Integer),
        sa.Column('token', sa.Text),
        sa.Column('created_at', sa.Text, nullable=False),
    )

    op.create_table(
        'project_members',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('project_id', sa.Integer, sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('added_by', sa.Integer, sa.ForeignKey('users.id')),
        sa.Column('created_at', sa.Text, nullable=False),
        sa.UniqueConstraint('user_id', 'project_id'),
    )

    op.create_table(
        'previews',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('project_id', sa.Integer, sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('preview_name', sa.Text, nullable=False),
        sa.Column('url_hash', sa.Text, nullable=False),
        sa.Column('mr_id', sa.Integer),
        sa.Column('mr_title', sa.Text),
        sa.Column('branch', sa.Text, nullable=False),
        sa.Column('commit_sha', sa.Text, nullable=False),
        sa.Column('status', sa.Text, nullable=False, server_default="'creating'"),
        sa.Column('url', sa.Text, nullable=False),
        sa.Column('path', sa.Text, nullable=False),
        sa.Column('created_at', sa.Text, nullable=False),
        sa.Column('last_deployed_at', sa.Text),
        sa.Column('last_deployment_status', sa.Text),
        sa.Column('last_deployment_error', sa.Text),
        sa.Column('last_deployment_duration', sa.Integer),
        sa.Column('last_deployment_completed_at', sa.Text),
        sa.Column('last_accessed_at', sa.Text),
        sa.Column('auto_update', sa.Integer, nullable=False, server_default='1'),
        sa.Column('pinned', sa.Integer, nullable=False, server_default='0'),
        sa.Column('env_vars', sa.Text, server_default="'{}'"),
        sa.Column('vm_id', sa.Integer),
        sa.Column('vm_ip', sa.Text),
        sa.Column('volume_id', sa.Integer),
        sa.UniqueConstraint('project_id', 'preview_name'),
    )
    op.create_index('idx_previews_url_hash', 'previews', ['url_hash'])

    op.create_table(
        'deployments',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('preview_id', sa.Integer, sa.ForeignKey('previews.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.Text, nullable=False, server_default="'running'"),
        sa.Column('log_output', sa.Text, server_default="''"),
        sa.Column('error', sa.Text),
        sa.Column('triggered_by', sa.Text),
        sa.Column('started_at', sa.Text, nullable=False),
        sa.Column('completed_at', sa.Text),
        sa.Column('duration', sa.Integer),
    )

    op.create_table(
        'cloud_resources',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('project_id', sa.Integer, sa.ForeignKey('projects.id', ondelete='SET NULL')),
        sa.Column('preview_name', sa.Text, nullable=False),
        sa.Column('resource_type', sa.Text, nullable=False),
        sa.Column('resource_id', sa.Integer, nullable=False),
        sa.Column('resource_name', sa.Text, nullable=False),
        sa.Column('spec', sa.Text, server_default="'{}'"),
        sa.Column('price_hourly', sa.Float, server_default='0'),
        sa.Column('price_monthly', sa.Float, server_default='0'),
        sa.Column('created_at', sa.Text, nullable=False),
        sa.Column('destroyed_at', sa.Text),
        sa.Column('duration_seconds', sa.Integer),
    )


def downgrade() -> None:
    op.drop_table('cloud_resources')
    op.drop_table('deployments')
    op.drop_index('idx_previews_url_hash')
    op.drop_table('previews')
    op.drop_table('project_members')
    op.drop_table('cli_auth_requests')
    op.drop_table('api_tokens')
    op.drop_table('org_invitations')
    op.drop_table('projects')
    op.drop_table('org_email_domains')
    op.drop_table('org_members')
    op.drop_table('organizations')
    op.drop_table('sessions')
    op.drop_table('oauth_accounts')
    op.drop_table('users')
