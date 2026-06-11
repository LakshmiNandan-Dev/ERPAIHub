"""Add SYSTEM / WebLogic / apps-OS credentials to ebs_environments (adop patching)

Revision ID: c8e2f5a91d04
Revises: b7d4e9f10c63
Create Date: 2026-06-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8e2f5a91d04'
down_revision: Union[str, Sequence[str], None] = 'b7d4e9f10c63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ebs_environments', sa.Column('system_user', sa.String(length=100),
                                                server_default='system', nullable=True))
    op.add_column('ebs_environments', sa.Column('system_password_enc', sa.Text(), nullable=True))
    op.add_column('ebs_environments', sa.Column('weblogic_user', sa.String(length=100),
                                                server_default='weblogic', nullable=True))
    op.add_column('ebs_environments', sa.Column('weblogic_password_enc', sa.Text(), nullable=True))
    op.add_column('ebs_environments', sa.Column('apps_os_user', sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column('ebs_environments', 'apps_os_user')
    op.drop_column('ebs_environments', 'weblogic_password_enc')
    op.drop_column('ebs_environments', 'weblogic_user')
    op.drop_column('ebs_environments', 'system_password_enc')
    op.drop_column('ebs_environments', 'system_user')
