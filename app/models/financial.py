from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.equipment import EquipmentUnit
    from app.models.location import Location


class MachineProForma(Base):
    __tablename__ = "machine_proformas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Capital inputs
    machine_cost: Mapped[float] = mapped_column(Float, nullable=False)
    installation_cost: Mapped[float] = mapped_column(Float, default=0.0)
    initial_inventory_cost: Mapped[float] = mapped_column(Float, default=0.0)

    # Revenue inputs
    daily_transactions: Mapped[float] = mapped_column(Float, nullable=False)
    # How the transaction count was entered ("daily" or "weekly"). daily_transactions always
    # stores the canonical per-day figure; this just drives the input unit on the edit form.
    transaction_basis: Mapped[str] = mapped_column(String(10), default="daily")
    avg_ticket_usd: Mapped[float] = mapped_column(Float, nullable=False)
    cogs_pct: Mapped[float] = mapped_column(Float, nullable=False)

    # Monthly operating costs
    commission_pct: Mapped[float] = mapped_column(Float, default=0.0)
    restock_labor_monthly: Mapped[float] = mapped_column(Float, default=0.0)
    supplies_monthly: Mapped[float] = mapped_column(Float, default=0.0)
    insurance_monthly: Mapped[float] = mapped_column(Float, default=0.0)
    connectivity_monthly: Mapped[float] = mapped_column(Float, default=0.0)
    software_monthly: Mapped[float] = mapped_column(Float, default=0.0)  # flat platform / SaaS fee
    other_opex_monthly: Mapped[float] = mapped_column(Float, default=0.0)
    # Optional flat monthly machine loan/lease payment entered directly. When > 0 it overrides
    # the APR/term amortization below as the financing cost; either way it counts as a monthly
    # operating cost and marks the machine as financed (excluded from upfront investment).
    finance_payment_monthly: Mapped[float] = mapped_column(Float, default=0.0)

    # Per-transaction SaaS / payment-processing cost. Cantaloupe/365 et al. bill a revenue
    # share PLUS a flat fee per swipe, so both are modeled: processing_fee_pct is a fraction
    # of revenue (0.0595 = 5.95%), processing_fee_per_txn is a flat dollar amount per sale.
    processing_fee_pct: Mapped[float] = mapped_column(Float, default=0.0)
    processing_fee_per_txn: Mapped[float] = mapped_column(Float, default=0.0)

    # Machine financing. When finance_term_months > 0 the machine is treated as financed:
    # an amortized monthly loan payment (machine_cost @ finance_apr_pct over the term) is
    # added to monthly operating costs, and the machine cost is excluded from the upfront
    # investment. finance_apr_pct is an annual fraction (0.08 = 8% APR). Both default to 0
    # (pay cash), so existing scenarios are unchanged.
    finance_apr_pct: Mapped[float] = mapped_column(Float, default=0.0)
    finance_term_months: Mapped[float] = mapped_column(Float, default=0.0)

    # JSON list of 12 monthly multipliers, e.g. [0.7, 0.8, ..., 1.2]; NULL = flat (all 1.0)
    seasonality_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    location: Mapped[Location | None] = relationship(back_populates="proformas")

    # The catalog unit this scenario was seeded from. Costs are snapshotted into the columns
    # above (editable, frozen), so this is a soft reference for a "Based on …" link and an
    # optional manual re-pull — never a live binding that would retro-edit saved scenarios.
    equipment_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("equipment_units.id"), nullable=True
    )
    equipment_unit: Mapped[EquipmentUnit | None] = relationship()
