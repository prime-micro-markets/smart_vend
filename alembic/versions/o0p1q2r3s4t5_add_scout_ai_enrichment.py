"""add AI enrichment columns to scouted_locations (Scout Map Phase 2)

Revision ID: o0p1q2r3s4t5
Revises: n9o0p1q2r3s4
Create Date: 2026-06-05 00:00:00.000000

Chained onto the single head ``n9o0p1q2r3s4`` so there stays exactly one linear
head (see the repo's multiple-heads gotcha). Adds the columns the Phase 2 AI
deep-dive (agent.run_scout_enrich_job) writes back onto a scouted business.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "o0p1q2r3s4t5"
down_revision: Union[str, Sequence[str], None] = "n9o0p1q2r3s4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = [
    ("ai_status", sa.String(length=20)),
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
    for name, col_type in _COLUMNS:
        op.add_column("scouted_locations", sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("scouted_locations", name)
