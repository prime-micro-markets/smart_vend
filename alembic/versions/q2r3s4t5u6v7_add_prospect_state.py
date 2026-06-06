"""add state column to prospects; widen ai_has_vending

Revision ID: q2r3s4t5u6v7
Revises: p1q2r3s4t5u6
Create Date: 2026-06-06 00:00:00.000000

Chained onto the single head ``p1q2r3s4t5u6`` so there stays exactly one linear
head. Adds the contact-card ``state`` field (mirrors ``locations.state``) and
widens ``ai_has_vending`` so the AI deep-dive verdict is no longer truncated in
the contact card.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "q2r3s4t5u6v7"
down_revision: Union[str, Sequence[str], None] = "p1q2r3s4t5u6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("prospects", sa.Column("state", sa.String(length=2), nullable=True))
    # Widen so the AI vending verdict isn't clipped mid-sentence (was 160).
    with op.batch_alter_table("prospects") as batch:
        batch.alter_column(
            "ai_has_vending",
            existing_type=sa.String(length=160),
            type_=sa.String(length=400),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("prospects") as batch:
        batch.alter_column(
            "ai_has_vending",
            existing_type=sa.String(length=400),
            type_=sa.String(length=160),
            existing_nullable=True,
        )
    op.drop_column("prospects", "state")
