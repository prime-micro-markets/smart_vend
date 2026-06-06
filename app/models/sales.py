from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.location import Location


class Prospect(Base):
    __tablename__ = "prospects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    contact_title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    venue_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    city: Mapped[str] = mapped_column(String(100), default="Panama City")
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    pipeline_stage: Mapped[str] = mapped_column(String(30), default="lead")
    tier: Mapped[str | None] = mapped_column(String(5), nullable=True)
    foot_traffic_estimate: Mapped[str | None] = mapped_column(String(20), nullable=True)
    estimated_machines: Mapped[int] = mapped_column(Integer, default=1)
    next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_action_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    lost_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    website: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    template_draft_subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    template_draft_body: Mapped[str | None] = mapped_column(Text, nullable=True)

    # AI deep-dive (filled by agent.run_prospect_enrich_job, mirrors ScoutedLocation).
    # ai_status: none | pending | running | done | error.
    ai_status: Mapped[str] = mapped_column(String(20), default="none")
    ai_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_employees: Mapped[str | None] = mapped_column(String(60), nullable=True)
    ai_foot_traffic: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ai_contact_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    ai_contact_title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ai_contact_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ai_contact_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ai_has_vending: Mapped[str | None] = mapped_column(String(400), nullable=True)
    ai_researched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    location: Mapped[Location | None] = relationship(back_populates="prospects")
    outreach_logs: Mapped[list[OutreachLog]] = relationship(
        back_populates="prospect",
        order_by="OutreachLog.contacted_at.desc()",
        cascade="all, delete-orphan",
    )


class OutreachLog(Base):
    __tablename__ = "outreach_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id"), nullable=False)
    prospect: Mapped[Prospect] = relationship(back_populates="outreach_logs")
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), default="outbound")
    contacted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    subject_or_summary: Mapped[str | None] = mapped_column(String(300), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(50), nullable=True)
    follow_up_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
