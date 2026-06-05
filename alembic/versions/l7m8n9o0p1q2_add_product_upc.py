"""add products.upc (barcode for market-reference lookups)

Revision ID: l7m8n9o0p1q2
Revises: k6l7m8n9o0p1
Create Date: 2026-06-05 00:00:00.000000

Note: an earlier draft of this migration reused the revision id
``h3i4j5k6l7m8``, which collided with the proforma-financing migration that
already owned that id on main. It is re-issued here with a unique id chained
onto the current head (``k6l7m8n9o0p1``) so there is a single linear head.

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "l7m8n9o0p1q2"
down_revision: Union[str, Sequence[str], None] = "k6l7m8n9o0p1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # UPC/EAN barcode used only to key market-reference pricing (UPCitemdb,
    # Open Prices). Nullable; not part of cost/margin math.
    op.add_column("products", sa.Column("upc", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "upc")
