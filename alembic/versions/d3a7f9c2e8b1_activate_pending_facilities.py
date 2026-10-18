"""activate facilities left pending review

Facilities no longer need a super admin's approval; they are live as soon
as they're created. Ones created while approval existed, and never
approved, are activated here.

Revision ID: d3a7f9c2e8b1
Revises: c5e8a1d4f7b2
Create Date: 2026-10-18 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd3a7f9c2e8b1'
down_revision: Union[str, Sequence[str], None] = 'c5e8a1d4f7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Until now a facility was only ever inactive while pending review.
    op.execute('UPDATE facilities SET is_active = true WHERE is_active = false')


def downgrade() -> None:
    """Downgrade schema. No-op: which facilities were pending isn't recorded."""
