"""Add patch_runs table (EBS patching agent — DB/Grid/RAC OPatch + adop + WebLogic/FMW)

Revision ID: b7d4e9f10c63
Revises: a3f7d1c905e2
Create Date: 2026-06-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = 'b7d4e9f10c63'
down_revision: Union[str, Sequence[str], None] = 'a3f7d1c905e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'patch_runs',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('environment_id', sa.Integer(),
                  sa.ForeignKey('ebs_environments.id', ondelete='SET NULL'), nullable=True),
        sa.Column('environment_name', sa.String(length=50), nullable=True),
        sa.Column('patch_label', sa.String(length=255), nullable=True),
        sa.Column('patch_number', sa.String(length=255), nullable=True),
        sa.Column('components', JSONB(), nullable=True),
        sa.Column('params', JSONB(), nullable=True),
        sa.Column('steps', JSONB(), nullable=True),
        sa.Column('guard_status', sa.String(length=20), nullable=True),
        sa.Column('guard_result', JSONB(), nullable=True),
        sa.Column('override_reason', sa.Text(), nullable=True),
        sa.Column('overridden_by', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('status', sa.String(length=50), server_default='pending', nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('patch_runs')
