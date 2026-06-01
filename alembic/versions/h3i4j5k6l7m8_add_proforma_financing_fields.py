"""add_proforma_machine_financing_fields

Adds machine-financing inputs to machine_proformas: finance_apr_pct (annual rate
as a fraction) and finance_term_months (loan length). server_default backfills
existing rows with 0, so old scenarios stay "pay cash" and calculate identically.

Revision ID: h3i4j5k6l7m8
Revises: g2h3i4j5k6l7
Create Date: 2026-06-01 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "h3i4j5k6l7m8"
down_revision: Union[str, Sequence[str], None] = "g2h3i4j5k6l7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("machine_proformas") as batch:
        batch.add_column(
            sa.Column("finance_apr_pct", sa.Float(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("finance_term_months", sa.Float(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("machine_proformas") as batch:
        batch.drop_column("finance_term_months")
        batch.drop_column("finance_apr_pct")
