from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PriceResult:
    vendor_key: str
    vendor_name: str
    vendor_type: str  # "online_wholesale" | "online_vending" | "local_wholesale" | "local_retail"
    product_name: str
    unit_price: float | None = None
    case_price: float | None = None
    case_qty: int | None = None
    unit_size: str | None = None
    url: str | None = None
    in_stock: bool | None = None
    min_order: str | None = None
    notes: str = ""
    source: str = "scrape"  # "api" | "scrape" | "ai_search"
    confidence: str = "medium"  # "high" | "medium" | "low"
    # True when the unit price was derived from case math rather than scraped
    # directly. UI surfaces a small "calc" badge so the operator knows.
    unit_price_derived: bool = False
    case_price_derived: bool = False

    def normalize(self, *, fallback_case_pack: int | None = None) -> None:
        """Backfill unit_price <-> case_price math when we have enough info.

        Wholesale fetchers (WebstaurantStore) often return case_price + case_qty
        with unit_price=None. Retail fetchers (Sam's Club, Walmart) return
        unit_price but no case fields. If the bound Product knows its
        case_pack_qty, pass that as ``fallback_case_pack`` so the dispatcher
        can derive the missing side for cross-vendor comparison.

        Operates in place. Already-set values are never overwritten.
        """
        qty = self.case_qty or fallback_case_pack
        if self.unit_price is None and self.case_price is not None and qty:
            self.unit_price = round(self.case_price / qty, 4)
            self.unit_price_derived = True
        if self.case_price is None and self.unit_price is not None and qty:
            self.case_price = round(self.unit_price * qty, 2)
            self.case_price_derived = True
            if not self.case_qty:
                self.case_qty = qty

    def is_empty(self) -> bool:
        """A row with no price, no case data, and no URL carries no information.

        The dispatcher drops these before rendering — they only ever produced
        a $— $— $— $— row in the results table and confused the operator.
        """
        return self.unit_price is None and self.case_price is None and not self.url

    def to_dict(self) -> dict:
        return {
            "vendor_key": self.vendor_key,
            "vendor_name": self.vendor_name,
            "vendor_type": self.vendor_type,
            "product_name": self.product_name,
            "unit_price": self.unit_price,
            "case_price": self.case_price,
            "case_qty": self.case_qty,
            "unit_size": self.unit_size,
            "url": self.url,
            "in_stock": self.in_stock,
            "min_order": self.min_order,
            "notes": self.notes,
            "source": self.source,
            "confidence": self.confidence,
            "unit_price_derived": self.unit_price_derived,
            "case_price_derived": self.case_price_derived,
        }


class FetchError(Exception):
    pass


VENDOR_META: dict[str, dict] = {
    "sams_club": {
        "label": "Sam's Club",
        "type": "local_wholesale",
        "icon": "bi-building",
        "color": "primary",
    },
    "webstaurantstore": {
        "label": "WebstaurantStore",
        "type": "online_wholesale",
        "icon": "bi-globe",
        "color": "success",
        # Reliable scrape only with Firecrawl; otherwise hit-or-miss. Flagged in
        # the comparator UI so the operator weighs its prices accordingly.
        "weak": True,
    },
    "vendors_supply": {
        "label": "Vendors Supply",
        "type": "online_vending",
        "icon": "bi-box",
        "color": "warning",
    },
    "candy_machines": {
        "label": "CandyMachines",
        "type": "online_vending",
        "icon": "bi-star",
        "color": "danger",
        # Vending-retail prices (not true wholesale) + noisy off-domain hits.
        "weak": True,
    },
}
