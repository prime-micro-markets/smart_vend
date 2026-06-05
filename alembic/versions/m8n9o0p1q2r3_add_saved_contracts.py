"""add saved_contracts (SAM.gov Gov Contracts favorites)

Revision ID: m8n9o0p1q2r3
Revises: l7m8n9o0p1q2
Create Date: 2026-06-05 00:00:00.000000

Chained onto the single head ``l7m8n9o0p1q2`` so there stays exactly one linear
head (see the repo's multiple-heads gotcha). Creates the table backing the
"Gov Contracts" tab's saved favorites.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "m8n9o0p1q2r3"
down_revision: Union[str, Sequence[str], None] = "l7m8n9o0p1q2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "saved_contracts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("notice_id", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("agency", sa.String(length=400), nullable=True),
        sa.Column("notice_type", sa.String(length=80), nullable=True),
        sa.Column("posted_date", sa.String(length=40), nullable=True),
        sa.Column("response_deadline", sa.String(length=60), nullable=True),
        sa.Column("set_aside", sa.String(length=120), nullable=True),
        sa.Column("naics_code", sa.String(length=20), nullable=True),
        sa.Column("place", sa.String(length=200), nullable=True),
        sa.Column("solicitation_number", sa.String(length=120), nullable=True),
        sa.Column("ui_link", sa.String(length=600), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("saved_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_saved_contracts_notice_id", "saved_contracts", ["notice_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_saved_contracts_notice_id", table_name="saved_contracts")
    op.drop_table("saved_contracts")
