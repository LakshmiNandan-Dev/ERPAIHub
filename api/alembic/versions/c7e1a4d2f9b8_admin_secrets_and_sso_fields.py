"""Admin console: user admin/SSO fields, encrypted-secret resource tables

Adds is_admin / auth_provider / external_id to users (password_hash now
nullable), the admin-managed ssh_servers, ebs_environments and llm_credentials
tables (secret columns hold Fernet ciphertext), and environment/ssh references
on deployment_runs.

Revision ID: c7e1a4d2f9b8
Revises: a1f3c8e92b47
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = 'c7e1a4d2f9b8'
down_revision: Union[str, Sequence[str], None] = 'a1f3c8e92b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── users: admin + SSO fields ──────────────────────────────────────────────
    op.add_column('users', sa.Column('is_admin', sa.Boolean(), server_default=sa.text('FALSE'), nullable=False))
    op.add_column('users', sa.Column('auth_provider', sa.String(length=50), server_default='local', nullable=False))
    op.add_column('users', sa.Column('external_id', sa.String(length=255), nullable=True))
    op.create_unique_constraint('uq_users_external_id', 'users', ['external_id'])
    op.alter_column('users', 'password_hash', existing_type=sa.String(length=255), nullable=True)

    # ── ssh_servers ────────────────────────────────────────────────────────────
    op.create_table(
        'ssh_servers',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False, unique=True),
        sa.Column('hostname', sa.String(length=255), nullable=False),
        sa.Column('port', sa.Integer(), server_default='22', nullable=False),
        sa.Column('username', sa.String(length=100), nullable=True),
        sa.Column('password_enc', sa.Text(), nullable=True),
        sa.Column('server_type', sa.String(length=50), server_default='application', nullable=False),
        sa.Column('app_services', JSONB(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('TRUE'), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # ── ebs_environments ───────────────────────────────────────────────────────
    op.create_table(
        'ebs_environments',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('name', sa.String(length=50), nullable=False, unique=True),
        sa.Column('db_host', sa.String(length=255), nullable=True),
        sa.Column('db_port', sa.Integer(), server_default='1521', nullable=False),
        sa.Column('db_sid', sa.String(length=100), nullable=True),
        sa.Column('db_user', sa.String(length=100), server_default='apps', nullable=True),
        sa.Column('db_password_enc', sa.Text(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('ssh_server_id', sa.Integer(), sa.ForeignKey('ssh_servers.id', ondelete='SET NULL'), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('TRUE'), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # ── llm_credentials ────────────────────────────────────────────────────────
    op.create_table(
        'llm_credentials',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('label', sa.String(length=100), nullable=True),
        sa.Column('model', sa.String(length=150), nullable=True),
        sa.Column('api_key_enc', sa.Text(), nullable=True),
        sa.Column('base_url', sa.String(length=512), nullable=True),
        sa.Column('is_default', sa.Boolean(), server_default=sa.text('FALSE'), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('TRUE'), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # ── deployment_runs references to managed resources ────────────────────────
    op.add_column('deployment_runs', sa.Column('ssh_server_id', sa.Integer(), nullable=True))
    op.add_column('deployment_runs', sa.Column('environment_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_deployment_runs_ssh_server', 'deployment_runs', 'ssh_servers',
                          ['ssh_server_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('fk_deployment_runs_environment', 'deployment_runs', 'ebs_environments',
                          ['environment_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_deployment_runs_environment', 'deployment_runs', type_='foreignkey')
    op.drop_constraint('fk_deployment_runs_ssh_server', 'deployment_runs', type_='foreignkey')
    op.drop_column('deployment_runs', 'environment_id')
    op.drop_column('deployment_runs', 'ssh_server_id')

    op.drop_table('llm_credentials')
    op.drop_table('ebs_environments')
    op.drop_table('ssh_servers')

    op.alter_column('users', 'password_hash', existing_type=sa.String(length=255), nullable=False)
    op.drop_constraint('uq_users_external_id', 'users', type_='unique')
    op.drop_column('users', 'external_id')
    op.drop_column('users', 'auth_provider')
    op.drop_column('users', 'is_admin')
