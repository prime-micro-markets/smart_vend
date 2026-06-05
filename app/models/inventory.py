from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)
    supplier_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    account_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    website: Mapped[str | None] = mapped_column(String(300), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Onboarding state — surfaces the "open these accounts" workflow on the
    # Suppliers tab. Lower `priority` sorts to the top of that banner.
    account_status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_started")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    products: Mapped[list[Product]] = relationship(back_populates="primary_supplier")

    @property
    def is_open(self) -> bool:
        return self.account_status == "open"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # UPC/EAN barcode — keys the market-reference lookups (UPCitemdb, Open
    # Prices). Reference-only; never used in cost/margin math.
    upc: Mapped[str | None] = mapped_column(String(20), nullable=True)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    unit_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_size: Mapped[str | None] = mapped_column(String(50), nullable=True)
    case_pack_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_seasonal: Mapped[bool] = mapped_column(Boolean, default=False)
    primary_supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("suppliers.id"), nullable=True
    )
    primary_supplier: Mapped[Supplier | None] = relationship(back_populates="products")
    restock_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    par_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    on_hand_qty: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    inventory_logs: Mapped[list[InventoryLog]] = relationship(back_populates="product")
    sources: Mapped[list[ProductSource]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
    )

    # ── derived sourcing helpers (mirror EquipmentUnit) ─────────────────────────
    @property
    def priced_sources(self) -> list[ProductSource]:
        """Sources that resolve to a usable per-unit cost, cheapest first."""
        return sorted(
            (s for s in self.sources if s.effective_unit_cost is not None),
            key=lambda s: s.effective_unit_cost,  # type: ignore[arg-type,return-value]
        )

    @property
    def best_source(self) -> ProductSource | None:
        """Cheapest supplier offer (per unit) — drives the catalog 'best cost' badge."""
        priced = self.priced_sources
        return priced[0] if priced else None

    @property
    def preferred_source(self) -> ProductSource | None:
        return next((s for s in self.sources if s.is_preferred), None)

    @property
    def buying_source(self) -> ProductSource | None:
        """Where the team should actually order from: pinned preferred, else cheapest."""
        return self.preferred_source or self.best_source

    @property
    def source_count(self) -> int:
        return len(self.sources)

    @property
    def sources_for_display(self) -> list[ProductSource]:
        """All sources, cheapest priced first, then any without a usable cost."""
        priced = self.priced_sources
        unpriced = [s for s in self.sources if s.effective_unit_cost is None]
        return priced + unpriced

    @property
    def best_unit_cost(self) -> float | None:
        """Cheapest per-unit cost across supplier offers. Derived (never stored), so
        the operator's hand-entered unit_cost is never overwritten and there is no
        denormalized value that can drift or go stale when a source is removed.
        Unlike EquipmentUnit (which denormalizes for cost filtering), the product
        catalog loads sources for the Best Source column anyway, so a column buys
        nothing here."""
        best = self.best_source
        return best.effective_unit_cost if best else None

    @property
    def effective_cost(self) -> float | None:
        """Cost used for margin and restock estimates: the cheapest supplier offer
        when one exists, otherwise the hand-entered unit_cost."""
        bc = self.best_unit_cost
        return bc if bc is not None else self.unit_cost

    @property
    def margin_pct(self) -> float | None:
        """Gross margin from sell_price vs effective_cost."""
        cost = self.effective_cost
        # sell_price truthy guards the division; cost == 0.0 is a valid (free) cost.
        if self.sell_price and cost is not None:
            return (self.sell_price - cost) / self.sell_price * 100
        return None

    @property
    def is_low_stock(self) -> bool:
        return self.par_level is not None and self.on_hand_qty < self.par_level

    @property
    def qty_needed(self) -> int:
        """Units below par — the restock shortfall (0 when at/above par or no par set)."""
        if self.par_level is None:
            return 0
        return max(0, self.par_level - self.on_hand_qty)


class ProductSource(Base):
    """One supplier's offer for one product — the basis for per-SKU price comparison.

    Vending buys by the case and sells by the unit, so the comparison key is the
    *per-unit* cost: an explicit ``unit_cost`` when set, otherwise
    ``case_price / case_pack_qty``. ``origin`` records where the offer came from
    (hand-entered, captured from the price comparator, or AI sourcing).
    """

    __tablename__ = "product_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    supplier_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    case_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    case_pack_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_size: Mapped[str | None] = mapped_column(String(50), nullable=True)
    min_order: Mapped[str | None] = mapped_column(String(100), nullable=True)
    price_notes: Mapped[str | None] = mapped_column(String(300), nullable=True)

    in_stock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stock_notes: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Team can pin a preferred supplier even when it isn't the absolute cheapest.
    is_preferred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    origin: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    # manual | comparator | ai_sourcing
    last_verified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)

    product: Mapped[Product] = relationship(back_populates="sources")
    supplier: Mapped[Supplier] = relationship()

    @property
    def effective_unit_cost(self) -> float | None:
        """Per-unit cost used for comparison. Vending buys by the case, so case math
        (case_price / case_pack_qty) wins when present; a bare unit_cost is the
        fallback for items sold singly. This keeps a comparator row that carries
        both a single-unit retail price and a bulk case price from overstating the
        true per-unit cost."""
        pack = self.case_pack_qty or 0
        if self.case_price is not None and pack > 0:
            return self.case_price / pack
        return self.unit_cost


class InventoryLog(Base):
    __tablename__ = "inventory_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    product: Mapped[Product] = relationship(back_populates="inventory_logs")
    machine_id: Mapped[int | None] = mapped_column(ForeignKey("machines.id"), nullable=True)
    log_type: Mapped[str] = mapped_column(String(20), nullable=False)
    qty_change: Mapped[int] = mapped_column(Integer, nullable=False)
    qty_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_cost_at_log: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    logged_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    logged_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
