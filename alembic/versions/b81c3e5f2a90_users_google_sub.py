"""users.google_sub for Google sign-in

Revision ID: b81c3e5f2a90
Revises: 4f2d9c1e7b3a
Create Date: 2026-10-18 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b81c3e5f2a90'
down_revision: Union[str, Sequence[str], None] = '4f2d9c1e7b3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('google_sub', sa.String(length=255), nullable=True))
    op.create_index(op.f('ix_users_google_sub'), 'users', ['google_sub'], unique=True)


def downgrade() -> None:
    """Downgrade schema. Google-only accounts stay, but can't sign in until
    they set a password with an invite."""
    op.drop_index(op.f('ix_users_google_sub'), table_name='users')
    op.drop_column('users', 'google_sub')
