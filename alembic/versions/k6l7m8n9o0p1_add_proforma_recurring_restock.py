"""add_proforma_recurring_restock

Adds recurring_restock_monthly to machine_proformas: a flat recurring monthly
product-replenishment budget (the ongoing counterpart to the one-time
initial_inventory_cost). Counts as a monthly operating cost. server_default
backfills existing rows with 0, so old scenarios are unchanged.

Revision ID: k6l7m8n9o0p1
Revises: j5k6l7m8n9o0
Create Date: 2026-06-01 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "k6l7m8n9o0p1"
down_revision: Union[str, Sequence[str], None] = "j5k6l7m8n9o0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("machine_proformas") as batch:
        batch.add_column(
            sa.Column(
                "recurring_restock_monthly", sa.Float(), nullable=False, server_default="0"
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("machine_proformas") as batch:
        batch.drop_column("recurring_restock_monthly")
