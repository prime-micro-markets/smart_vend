from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScoutedLocation(Base):
    """A business the operator saved from the Lead Gen "Scout Map" tab.

    Keeps the map coordinates + opportunity score alongside the OSM-derived
    details so saved rows survive reloads without re-querying Overpass. Keyed by
    ``osm_id`` (e.g. ``"node/12345"``) so saving the same place twice is
    idempotent. ``status`` flips ``scouted`` → ``promoted`` when the operator
    promotes it into the sales pipeline (creating a ``Prospect``)."""

    __tablename__ = "scouted_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    osm_id: Mapped[str] = mapped_column(String(60), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    venue_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    website: Mapped[str | None] = mapped_column(String(300), nullable=True)
    opportunity_score: Mapped[int] = mapped_column(Integer, default=0)
    nearest_food_mi: Mapped[float | None] = mapped_column(Float, nullable=True)
    has_vending_guess: Mapped[str] = mapped_column(String(20), default="unknown")
    signals: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of reason tags
    status: Mapped[str] = mapped_column(String(20), default="scouted")  # scouted | promoted
    promoted_prospect_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 2 AI deep-dive (filled by agent.run_scout_enrich_job). ai_status:
    # none | pending | running | done | error.
    ai_status: Mapped[str] = mapped_column(String(20), default="none")
    ai_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_employees: Mapped[str | None] = mapped_column(String(60), nullable=True)
    ai_foot_traffic: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ai_contact_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    ai_contact_title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ai_contact_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ai_contact_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ai_has_vending: Mapped[str | None] = mapped_column(String(160), nullable=True)
    ai_researched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    saved_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
