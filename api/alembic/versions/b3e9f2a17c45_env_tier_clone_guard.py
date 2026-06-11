"""Environment tier/identity + clone production-guard fields

Revision ID: b3e9f2a17c45
Revises: a2d7c4e8f190
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = 'b3e9f2a17c45'
down_revision: Union[str, Sequence[str], None] = 'a2d7c4e8f190'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ebs_environments', sa.Column('tier', sa.String(length=20), server_default='nonprod', nullable=False))
    op.add_column('ebs_environments', sa.Column('db_id', sa.String(length=40), nullable=True))
    op.add_column('ebs_environments', sa.Column('global_name', sa.String(length=255), nullable=True))

    op.add_column('clone_runs', sa.Column('target_environment_id', sa.Integer(),
                                          sa.ForeignKey('ebs_environments.id', ondelete='SET NULL'), nullable=True))
    op.add_column('clone_runs', sa.Column('guard_status', sa.String(length=20), nullable=True))
    op.add_column('clone_runs', sa.Column('guard_result', JSONB(), nullable=True))
    op.add_column('clone_runs', sa.Column('override_reason', sa.Text(), nullable=True))
    op.add_column('clone_runs', sa.Column('overridden_by', sa.Integer(),
                                          sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True))


def downgrade() -> None:
    op.drop_column('clone_runs', 'overridden_by')
    op.drop_column('clone_runs', 'override_reason')
    op.drop_column('clone_runs', 'guard_result')
    op.drop_column('clone_runs', 'guard_status')
    op.drop_column('clone_runs', 'target_environment_id')
    op.drop_column('ebs_environments', 'global_name')
    op.drop_column('ebs_environments', 'db_id')
    op.drop_column('ebs_environments', 'tier')
