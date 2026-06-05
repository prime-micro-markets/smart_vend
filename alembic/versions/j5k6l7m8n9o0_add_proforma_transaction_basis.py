"""add_proforma_transaction_basis

Adds transaction_basis to machine_proformas so the calculator can accept the
transaction count as daily or weekly. daily_transactions stays the canonical
per-day figure; this column only records the input unit for the edit form.
server_default "daily" backfills existing rows, leaving them unchanged.

Revision ID: j5k6l7m8n9o0
Revises: i4j5k6l7m8n9
Create Date: 2026-06-01 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "j5k6l7m8n9o0"
down_revision: Union[str, Sequence[str], None] = "i4j5k6l7m8n9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("machine_proformas") as batch:
        batch.add_column(
            sa.Column(
                "transaction_basis",
                sa.String(length=10),
                nullable=False,
                server_default="daily",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("machine_proformas") as batch:
        batch.drop_column("transaction_basis")
