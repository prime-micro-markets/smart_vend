from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SavedContract(Base):
    """A SAM.gov contract opportunity the operator saved from the Gov Contracts
    tab. Mirrors the displayed fields of ``sam_gov.SamOpportunity`` so saved rows
    survive page reloads without re-querying the API. Keyed by SAM's ``notice_id``
    (unique) so saving the same opportunity twice is idempotent."""

    __tablename__ = "saved_contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    notice_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    agency: Mapped[str | None] = mapped_column(String(400), nullable=True)
    notice_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    posted_date: Mapped[str | None] = mapped_column(String(40), nullable=True)
    response_deadline: Mapped[str | None] = mapped_column(String(60), nullable=True)
    set_aside: Mapped[str | None] = mapped_column(String(120), nullable=True)
    naics_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    place: Mapped[str | None] = mapped_column(String(200), nullable=True)
    solicitation_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ui_link: Mapped[str | None] = mapped_column(String(600), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    saved_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
