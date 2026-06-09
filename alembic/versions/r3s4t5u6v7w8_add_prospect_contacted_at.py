"""add contacted_at column to prospects

Revision ID: r3s4t5u6v7w8
Revises: q2r3s4t5u6v7
Create Date: 2026-06-09 00:00:00.000000

Chained onto the single head ``q2r3s4t5u6v7`` so there stays exactly one linear
head. Adds the first-contact date so the sales pipeline can show when a prospect
was emailed a proposal and filter the Contacted column by that date.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r3s4t5u6v7w8"
down_revision: Union[str, Sequence[str], None] = "q2r3s4t5u6v7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("prospects", sa.Column("contacted_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("prospects", "contacted_at")
