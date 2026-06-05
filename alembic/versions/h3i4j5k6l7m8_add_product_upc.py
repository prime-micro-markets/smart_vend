"""add products.upc (barcode for market-reference lookups)

Revision ID: h3i4j5k6l7m8
Revises: g2h3i4j5k6l7
Create Date: 2026-06-05 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "h3i4j5k6l7m8"
down_revision: Union[str, Sequence[str], None] = "g2h3i4j5k6l7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # UPC/EAN barcode used only to key market-reference pricing (UPCitemdb,
    # Open Prices). Nullable; not part of cost/margin math.
    op.add_column("products", sa.Column("upc", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "upc")
