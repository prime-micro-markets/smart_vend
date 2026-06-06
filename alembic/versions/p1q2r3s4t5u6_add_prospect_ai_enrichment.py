"""add AI enrichment columns to prospects (pipeline card deep-dive)

Revision ID: p1q2r3s4t5u6
Revises: o0p1q2r3s4t5
Create Date: 2026-06-05 00:00:00.000000

Chained onto the single head ``o0p1q2r3s4t5`` so there stays exactly one linear
head (see the repo's multiple-heads gotcha). Mirrors the scout enrichment
columns onto ``prospects`` so the sales-pipeline card can run the same Claude +
web-search deep-dive (agent.run_prospect_enrich_job).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "p1q2r3s4t5u6"
down_revision: Union[str, Sequence[str], None] = "o0p1q2r3s4t5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = [
    ("ai_job_id", sa.Integer()),
    ("ai_summary", sa.Text()),
    ("ai_employees", sa.String(length=60)),
    ("ai_foot_traffic", sa.String(length=20)),
    ("ai_contact_name", sa.String(length=150)),
    ("ai_contact_title", sa.String(length=100)),
    ("ai_contact_email", sa.String(length=200)),
    ("ai_contact_phone", sa.String(length=40)),
    ("ai_has_vending", sa.String(length=160)),
    ("ai_researched_at", sa.DateTime()),
]


def upgrade() -> None:
    # ai_status carries a server default so existing rows read as "none" (the
    # start state the UI expects) rather than NULL.
    op.add_column(
        "prospects",
        sa.Column("ai_status", sa.String(length=20), nullable=True, server_default="none"),
    )
    for name, col_type in _COLUMNS:
        op.add_column("prospects", sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("prospects", name)
    op.drop_column("prospects", "ai_status")
