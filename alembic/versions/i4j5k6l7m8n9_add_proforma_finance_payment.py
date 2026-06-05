"""add_proforma_finance_payment_monthly

Adds an optional flat monthly machine finance/lease payment to machine_proformas.
When > 0 it overrides the APR/term amortization as the financing cost; either way
it is a monthly operating cost. server_default backfills existing rows with 0, so
old scenarios are unchanged.

Revision ID: i4j5k6l7m8n9
Revises: h3i4j5k6l7m8
Create Date: 2026-06-01 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i4j5k6l7m8n9"
down_revision: Union[str, Sequence[str], None] = "h3i4j5k6l7m8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("machine_proformas") as batch:
        batch.add_column(
            sa.Column(
                "finance_payment_monthly", sa.Float(), nullable=False, server_default="0"
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("machine_proformas") as batch:
        batch.drop_column("finance_payment_monthly")
