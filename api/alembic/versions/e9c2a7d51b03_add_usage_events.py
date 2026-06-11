"""Add usage_events telemetry table

Revision ID: e9c2a7d51b03
Revises: d4b8f1027e6a
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e9c2a7d51b03'
down_revision: Union[str, Sequence[str], None] = 'd4b8f1027e6a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'usage_events',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('username', sa.String(length=100), nullable=True),
        sa.Column('endpoint', sa.String(length=255), nullable=True),
        sa.Column('method', sa.String(length=10), nullable=True),
        sa.Column('provider', sa.String(length=50), nullable=True),
        sa.Column('model', sa.String(length=150), nullable=True),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.Column('request_bytes', sa.Integer(), server_default='0', nullable=False),
        sa.Column('response_bytes', sa.Integer(), server_default='0', nullable=False),
        sa.Column('prompt_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('completion_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('total_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('context_chars', sa.Integer(), server_default='0', nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('ix_usage_events_created_at', 'usage_events', ['created_at'])
    op.create_index('ix_usage_events_user_id', 'usage_events', ['user_id'])
    op.create_index('ix_usage_events_user_created', 'usage_events', ['user_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_usage_events_user_created', table_name='usage_events')
    op.drop_index('ix_usage_events_user_id', table_name='usage_events')
    op.drop_index('ix_usage_events_created_at', table_name='usage_events')
    op.drop_table('usage_events')
