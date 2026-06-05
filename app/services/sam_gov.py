"""SAM.gov contract-opportunity search for the Lead Gen "Gov Contracts" tab.

Thin, defensive client over the public SAM.gov Get Opportunities API. Like the
market-reference fetchers, every call is best-effort: a missing API key, a
rate-limit, a bad response, or a network failure degrades to an empty result
(and a `reason` string for the UI) instead of raising and breaking the page.

API: GET https://api.sam.gov/opportunities/v2/search
  • Required: api_key, postedFrom, postedTo  (format MM/dd/yyyy, max 1-year span)
  • Optional: title, state, zip, ncode (NAICS ≤6), typeOfSetAside, ptype
    (notice type), limit (≤1000), offset
  • Response: { totalRecords, opportunitiesData: [...] }
Docs: https://open.gsa.gov/api/get-opportunities-public-api/
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 12.0
_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
_DATE_FMT = "%m/%d/%Y"
_MAX_RANGE_DAYS = 365  # API hard limit on postedFrom..postedTo
_DEFAULT_LOOKBACK_DAYS = 90

# Vending Machine Operators — sensible default for a smart-cooler vending company.
DEFAULT_NAICS = "454210"

# Set-aside codes (typeOfSetAside). Veteran options are surfaced first in the UI
# since Prime Micro Markets is veteran-owned / pursuing VOB certification.
VETERAN_SET_ASIDES: list[tuple[str, str]] = [
    ("SDVOSBC", "Service-Disabled Veteran-Owned SB Set-Aside"),
    ("SDVOSBS", "Service-Disabled Veteran-Owned SB Sole Source"),
    ("VSA", "Veteran-Owned SB Set-Aside (VA)"),
    ("VSS", "Veteran-Owned SB Sole Source (VA)"),
]
OTHER_SET_ASIDES: list[tuple[str, str]] = [
    ("SBA", "Total Small Business Set-Aside"),
    ("SBP", "Partial Small Business Set-Aside"),
    ("8A", "8(a) Set-Aside"),
    ("8AN", "8(a) Sole Source"),
    ("HZC", "HUBZone Set-Aside"),
    ("HZS", "HUBZone Sole Source"),
    ("WOSB", "Women-Owned Small Business"),
    ("WOSBSS", "Women-Owned SB Sole Source"),
    ("EDWOSB", "Economically Disadvantaged WOSB"),
    ("EDWOSBSS", "EDWOSB Sole Source"),
]

# Notice types (ptype).
NOTICE_TYPES: list[tuple[str, str]] = [
    ("o", "Solicitation"),
    ("k", "Combined Synopsis/Solicitation"),
    ("p", "Presolicitation"),
    ("r", "Sources Sought"),
    ("s", "Special Notice"),
    ("a", "Award Notice"),
    ("i", "Intent to Bundle Requirements"),
    ("g", "Sale of Surplus Property"),
    ("u", "Justification (J&A)"),
]


@dataclass
class SamOpportunity:
    """One contract opportunity, normalized for display."""

    notice_id: str
    title: str
    agency: str = ""
    notice_type: str = ""
    posted_date: str = ""
    response_deadline: str = ""
    set_aside: str = ""
    naics_code: str = ""
    place: str = ""
    solicitation_number: str = ""
    ui_link: str = ""

    def has_data(self) -> bool:
        return bool(self.notice_id and self.title)


def _fmt_place(raw: dict | None) -> str:
    """Build a 'City, ST ZIP' string from a placeOfPerformance object."""
    if not isinstance(raw, dict):
        return ""
    city = (raw.get("city") or {}).get("name") or ""
    state = (raw.get("state") or {}).get("code") or (raw.get("state") or {}).get("name") or ""
    zip_code = raw.get("zip") or ""
    parts = [p for p in (city, state) if p]
    line = ", ".join(parts)
    if zip_code:
        line = f"{line} {zip_code}".strip()
    return line.strip(", ").strip()


def _parse_one(raw: dict) -> SamOpportunity | None:
    """Tolerantly map one opportunitiesData item to a SamOpportunity."""
    if not isinstance(raw, dict):
        return None
    notice_id = str(raw.get("noticeId") or "")
    title = str(raw.get("title") or "").strip()
    if not (notice_id and title):
        return None
    return SamOpportunity(
        notice_id=notice_id,
        title=title,
        agency=str(raw.get("fullParentPathName") or raw.get("organizationName") or "").strip(),
        notice_type=str(raw.get("type") or "").strip(),
        posted_date=str(raw.get("postedDate") or "").strip(),
        response_deadline=str(raw.get("responseDeadLine") or "").strip(),
        set_aside=str(
            raw.get("typeOfSetAsideDescription") or raw.get("typeOfSetAside") or ""
        ).strip(),
        naics_code=str(raw.get("naicsCode") or "").strip(),
        place=_fmt_place(raw.get("placeOfPerformance")),
        solicitation_number=str(raw.get("solicitationNumber") or "").strip(),
        ui_link=str(raw.get("uiLink") or "").strip(),
    )


def _date_window(posted_from: str, posted_to: str) -> tuple[str, str]:
    """Return MM/dd/yyyy (from, to), defaulting to the last ~90 days and clamping
    the span to the API's 1-year maximum so we never trigger a 400."""
    today = date.today()
    try:
        to_d = date.fromisoformat(posted_to) if posted_to else today
    except ValueError:
        to_d = today
    try:
        from_d = (
            date.fromisoformat(posted_from)
            if posted_from
            else to_d - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        )
    except ValueError:
        from_d = to_d - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
    if from_d > to_d:
        from_d, to_d = to_d, from_d
    if (to_d - from_d).days > _MAX_RANGE_DAYS:
        from_d = to_d - timedelta(days=_MAX_RANGE_DAYS)
    return from_d.strftime(_DATE_FMT), to_d.strftime(_DATE_FMT)


def search_opportunities(
    *,
    keyword: str = "",
    state: str = "",
    zip_code: str = "",
    naics: str = "",
    set_aside: str = "",
    notice_type: str = "",
    posted_from: str = "",
    posted_to: str = "",
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[SamOpportunity], int, str]:
    """Search SAM.gov opportunities.

    Returns ``(items, total_records, reason)``. ``reason`` is "" on success, or a
    short human-readable explanation when the result is empty because the key is
    missing or the API was unavailable (the UI shows it). Never raises.

    `posted_from`/`posted_to` accept ISO ``YYYY-MM-DD`` (HTML date inputs) and are
    converted to the MM/dd/yyyy the API requires.
    """
    if not settings.sam_gov_api_key:
        return [], 0, "not_configured"

    from_str, to_str = _date_window(posted_from, posted_to)
    params: dict[str, str | int] = {
        "api_key": settings.sam_gov_api_key,
        "postedFrom": from_str,
        "postedTo": to_str,
        "limit": max(1, min(int(limit or 25), 1000)),
        "offset": max(0, int(offset or 0)),
    }
    if keyword.strip():
        params["title"] = keyword.strip()
    if state.strip():
        params["state"] = state.strip().upper()
    if zip_code.strip():
        params["zip"] = zip_code.strip()
    if naics.strip():
        params["ncode"] = naics.strip()
    if set_aside.strip():
        params["typeOfSetAside"] = set_aside.strip()
    if notice_type.strip():
        params["ptype"] = notice_type.strip()

    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(_SEARCH_URL, params=params, headers={"Accept": "application/json"})
    except Exception:
        logger.warning("SAM.gov search request failed", exc_info=True)
        return [], 0, "unavailable"

    if resp.status_code != 200:
        # Don't leak the api_key (it's in params) — log status + a short body snippet.
        logger.info("SAM.gov search returned %s: %s", resp.status_code, resp.text[:300])
        return [], 0, "unavailable"

    try:
        data = resp.json()
    except ValueError:
        logger.warning("SAM.gov returned non-JSON body")
        return [], 0, "unavailable"

    raw_items = data.get("opportunitiesData") or []
    items = [op for op in (_parse_one(r) for r in raw_items) if op and op.has_data()]
    total = int(data.get("totalRecords") or len(items))
    return items, total, ""
