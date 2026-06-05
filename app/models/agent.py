from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentJob(Base):
    __tablename__ = "agent_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    input_params: Mapped[str | None] = mapped_column(Text, nullable=True)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id"), nullable=True)

    prospects_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prospects_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prospects_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    draft_subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    draft_body: Mapped[str | None] = mapped_column(Text, nullable=True)

    preview_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    tokens_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ratelimit_tokens_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ratelimit_tokens_reset: Mapped[str | None] = mapped_column(String(50), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_log: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
