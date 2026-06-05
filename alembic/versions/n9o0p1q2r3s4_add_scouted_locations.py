"""add scouted_locations (Lead Gen "Scout Map" saved businesses)

Revision ID: n9o0p1q2r3s4
Revises: m8n9o0p1q2r3
Create Date: 2026-06-05 00:00:00.000000

Chained onto the single head ``m8n9o0p1q2r3`` so there stays exactly one linear
head (see the repo's multiple-heads gotcha). Creates the table backing the
"Scout Map" tab's saved businesses.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "n9o0p1q2r3s4"
down_revision: Union[str, Sequence[str], None] = "m8n9o0p1q2r3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scouted_locations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("osm_id", sa.String(length=60), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("venue_type", sa.String(length=60), nullable=True),
        sa.Column("address", sa.String(length=300), nullable=True),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("phone", sa.String(length=40), nullable=True),
        sa.Column("website", sa.String(length=300), nullable=True),
        sa.Column("opportunity_score", sa.Integer(), nullable=True),
        sa.Column("nearest_food_mi", sa.Float(), nullable=True),
        sa.Column("has_vending_guess", sa.String(length=20), nullable=True),
        sa.Column("signals", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("promoted_prospect_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("saved_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_scouted_locations_osm_id", "scouted_locations", ["osm_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_scouted_locations_osm_id", table_name="scouted_locations")
    op.drop_table("scouted_locations")
