"""Add clone_runs table (EBS Rapid Clone agent)

Revision ID: a2d7c4e8f190
Revises: f1a6b3d92c84
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = 'a2d7c4e8f190'
down_revision: Union[str, Sequence[str], None] = 'f1a6b3d92c84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'clone_runs',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_name', sa.String(length=50), nullable=True),
        sa.Column('target_name', sa.String(length=50), nullable=True),
        sa.Column('source_sid', sa.String(length=100), nullable=True),
        sa.Column('target_sid', sa.String(length=100), nullable=True),
        sa.Column('source_db_host', sa.String(length=255), nullable=True),
        sa.Column('target_db_host', sa.String(length=255), nullable=True),
        sa.Column('source_apps_host', sa.String(length=255), nullable=True),
        sa.Column('target_apps_host', sa.String(length=255), nullable=True),
        sa.Column('db_method', sa.String(length=50), server_default='rman_duplicate', nullable=False),
        sa.Column('params', JSONB(), nullable=True),
        sa.Column('steps', JSONB(), nullable=True),
        sa.Column('status', sa.String(length=50), server_default='pending', nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('clone_runs')
