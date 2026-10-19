"""appointments cancellation_reason

Revision ID: e6b2d8f4a1c9
Revises: 25a07a1c6370
Create Date: 2026-10-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6b2d8f4a1c9'
down_revision: Union[str, Sequence[str], None] = '25a07a1c6370'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('appointments', sa.Column('cancellation_reason', sa.String(length=500), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('appointments', 'cancellation_reason')
