"""Parse a pasted Sam's Club purchase-history blob into priced line items.

Sam's blocks server-side price pulls (Akamai + datacenter IP), so the reliable
path is the operator's own logged-in browser. A no-token bookmarklet on
``samsclub.com/orders`` copies the order's line items to the clipboard; the
operator pastes that into the authenticated app, where these helpers turn the
paste into rows the review table can match against the catalog and save as
``Sam's Club`` ProductSource costs (the *actual paid price*, instant savings
included).

The parser is deliberately tolerant: the same paste box accepts the JSON the
bookmarklet emits, the CSV/TSV Sam's "Export" produces, or a plain copy of the
on-screen order table. It only needs an item name and a price per row.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass

# Header keywords → logical column. First hit wins, checked in order so a
# "unit price" column doesn't get grabbed as the line total.
_NAME_KEYS = ("item", "description", "product", "name", "title")
_PRICE_KEYS = ("price", "total", "amount", "paid", "cost", "subtotal")
_QTY_KEYS = ("qty", "quantity", "count", "pack", "units")

_MONEY = re.compile(r"-?\$?\s*([0-9][0-9,]*\.?[0-9]*)")


@dataclass
class SamsPasteLine:
    """One purchased line item lifted from the paste, pre-match."""

    name: str
    paid_price: float | None
    qty: int | None
    raw: str = ""


def _to_money(value: object) -> float | None:
    """Pull the first dollar figure out of a cell, tolerating ``$``/commas."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _MONEY.search(str(value))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _to_qty(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) or None
    m = re.search(r"[0-9]+", str(value))
    return int(m.group(0)) if m else None


def _first_key(row: dict, candidates: tuple[str, ...]) -> object:
    """Return the value of the first row key whose lowercased name contains one
    of ``candidates`` (substring match handles 'Item Description', 'Unit Qty')."""
    lowered = {(k or "").strip().lower(): v for k, v in row.items()}
    for cand in candidates:
        for key, val in lowered.items():
            if cand in key:
                return val
    return None


def _from_json(text: str) -> list[SamsPasteLine] | None:
    """Parse the bookmarklet's JSON. Accepts a bare array or ``{"items": [...]}``."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        data = data.get("items") or data.get("lines") or data.get("products") or []
    if not isinstance(data, list):
        return None
    lines: list[SamsPasteLine] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(_first_key(item, _NAME_KEYS) or "").strip()
        if not name:
            continue
        lines.append(
            SamsPasteLine(
                name=name,
                paid_price=_to_money(_first_key(item, _PRICE_KEYS)),
                qty=_to_qty(_first_key(item, _QTY_KEYS)),
                raw=json.dumps(item),
            )
        )
    return lines or None


def _looks_like_header(cells: list[str]) -> bool:
    joined = " ".join(cells).lower()
    return any(k in joined for k in _NAME_KEYS) and any(k in joined for k in _PRICE_KEYS)


def _from_delimited(text: str) -> list[SamsPasteLine]:
    """Parse CSV/TSV (Sam's export) or a copied table. Uses the header row to map
    columns when present, otherwise falls back to 'name ... price' per line."""
    # Tabs win when present (copied tables / TSV); else let csv sniff commas.
    delimiter = "\t" if "\t" in text else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return []

    header = rows[0]
    if _looks_like_header([c or "" for c in header]):
        keyed = (dict(zip(header, r, strict=False)) for r in rows[1:])
        lines: list[SamsPasteLine] = []
        for row in keyed:
            name = str(_first_key(row, _NAME_KEYS) or "").strip()
            if not name:
                continue
            lines.append(
                SamsPasteLine(
                    name=name,
                    paid_price=_to_money(_first_key(row, _PRICE_KEYS)),
                    qty=_to_qty(_first_key(row, _QTY_KEYS)),
                    raw=delimiter.join(c or "" for c in row.values()),
                )
            )
        return lines

    # Headerless: assume the longest text cell is the name and the last dollar
    # figure on the line is the price.
    lines = []
    for row in rows:
        cells = [(c or "").strip() for c in row if (c or "").strip()]
        if not cells:
            continue
        name = max(cells, key=len)
        price = next((_to_money(c) for c in reversed(cells) if _to_money(c) is not None), None)
        lines.append(
            SamsPasteLine(name=name, paid_price=price, qty=None, raw=delimiter.join(cells))
        )
    return lines


def parse_sams_paste(text: str) -> list[SamsPasteLine]:
    """Turn a pasted purchase-history blob into line items.

    Tries JSON first (the bookmarklet), then delimited text (export / copied
    table). Returns only rows that carry a name; price/qty may be ``None`` for
    the operator to fill on the review screen.
    """
    text = (text or "").strip()
    if not text:
        return []
    if text[0] in "[{":
        parsed = _from_json(text)
        if parsed is not None:
            return parsed
    return _from_delimited(text)
