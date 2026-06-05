"""Map-based placement scouting for the Lead Gen "Scout Map" tab.

Finds candidate businesses for smart-cooler placement around a point on a map and
scores each by how good an opportunity it looks. Like ``sam_gov.py`` and
``market_reference.py``, every network call is best-effort: a timeout, rate-limit,
or bad response degrades to an empty result + a ``reason`` string the UI can render,
never an exception that 500s the page.

Data sources (all free, no key required for the core flow):
  • Nominatim  — geocode a typed place/address to lat/lng.
  • Overpass   — OpenStreetMap businesses + nearby food/convenience/vending POIs.
Optional upgrade:
  • Google Places — richer phone/website/hours on drill-down when
    ``settings.google_places_api_key`` is set; otherwise we use the OSM record.

The opportunity score blends the four signals the operator asked for:
  (1) no food/convenience within ~3 mi (the strongest, fully-free signal),
  (2) business-type fit (gyms, offices, hospitals, hotels, industrial, etc.),
  (3) estimated size / foot-traffic  — placeholder until Phase 2 (AI enrichment),
  (4) likely-no-vending             — weak OSM ``vending_machine`` proximity guess.

Only two Overpass calls run per search (businesses + context POIs); nearest-food
distance is computed in-process with haversine, so per-business calls are avoided.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 25.0
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Nominatim/Overpass etiquette: identify the app in the User-Agent.
_UA = "PrimeMicroMarkets-SmartVend/1.0 (+https://primemicromarkets.com)"

_MILE_M = 1609.34
_FOOD_BUFFER_MI = 3.0  # "no food within 3 mi" radius around each business
_VENDING_NEAR_M = 75.0  # a vending_machine POI this close => "likely yes"
_MAX_RESULTS = 60
_MAX_RADIUS_MI = 25.0

# Default map center: Panama City, FL (Prime Micro Markets' home market).
DEFAULT_CENTER = (30.1588, -85.6602)
DEFAULT_RADIUS_MI = 3.0


# ── Venue types we hunt for, in priority order ──────────────────────────────
# Each maps a display/venue_type label to the OSM tag filters that select it and a
# 0-100 base "fit" weight (how good that venue type is for a smart cooler). The
# label is stored on ScoutedLocation.venue_type and copied to Prospect.venue_type
# on promote, so keep it human-readable.
#   filters: list of (key, value); value=None means "key exists with any value".
@dataclass(frozen=True)
class VenueDef:
    label: str
    fit: int
    filters: list[tuple[str, str | None]]


SCOUT_VENUES: list[VenueDef] = [
    VenueDef("gym / fitness", 90, [("leisure", "fitness_centre"), ("leisure", "sports_centre")]),
    VenueDef("corporate office", 85, [("office", None)]),
    VenueDef(
        "hospital / medical",
        85,
        [
            ("amenity", "hospital"),
            ("amenity", "clinic"),
            ("amenity", "doctors"),
            ("healthcare", None),
        ],
    ),
    VenueDef(
        "warehouse / industrial",
        85,
        [
            ("building", "warehouse"),
            ("building", "industrial"),
            ("industrial", None),
        ],
    ),
    VenueDef("hotel / motel", 80, [("tourism", "hotel"), ("tourism", "motel")]),
    VenueDef("university / college", 80, [("amenity", "university"), ("amenity", "college")]),
    VenueDef(
        "government / civic",
        75,
        [
            ("office", "government"),
            ("amenity", "townhall"),
            ("amenity", "courthouse"),
        ],
    ),
    VenueDef("auto dealer / service", 70, [("shop", "car"), ("shop", "car_repair")]),
    VenueDef(
        "retail / shopping",
        65,
        [
            ("shop", "mall"),
            ("shop", "department_store"),
            ("shop", "doityourself"),
        ],
    ),
    VenueDef("car wash", 60, [("amenity", "car_wash")]),
]
SCOUT_VENUE_LABELS = [v.label for v in SCOUT_VENUES]


@dataclass
class ScoutBiz:
    """One candidate business, normalized + scored for the map and results list."""

    osm_id: str  # e.g. "node/12345" — stable dedupe key
    name: str
    venue_type: str
    lat: float
    lng: float
    address: str = ""
    phone: str = ""
    website: str = ""
    has_vending_guess: str = "unknown"  # "likely yes" | "unknown"
    nearest_food_mi: float | None = None
    opportunity_score: int = 0
    fit_tags: list[str] = field(default_factory=list)
    enriched: bool = False  # True once Google Places filled contact fields


# ── Geocoding ───────────────────────────────────────────────────────────────
def geocode(query: str) -> tuple[float, float, str] | None:
    """Resolve a typed place/address to ``(lat, lng, label)`` via Nominatim.

    Returns ``None`` on empty input, no match, or any failure (caller falls back
    to the current/default center)."""
    q = (query or "").strip()
    if not q:
        return None
    params = {"q": q, "format": "jsonv2", "limit": 1, "countrycodes": "us"}
    try:
        with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
            resp = client.get(_NOMINATIM_URL, params=params)
        if resp.status_code != 200:
            return None
        rows = resp.json()
    except Exception:
        logger.warning("Nominatim geocode failed for %r", q, exc_info=True)
        return None
    if not rows:
        return None
    top = rows[0]
    try:
        return float(top["lat"]), float(top["lon"]), str(top.get("display_name") or q)
    except (KeyError, TypeError, ValueError):
        return None


# ── Overpass helpers ────────────────────────────────────────────────────────
def _overpass(query: str) -> list[dict] | None:
    """POST an Overpass QL query, return the ``elements`` list or None on failure."""
    try:
        with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
            resp = client.post(_OVERPASS_URL, data={"data": query})
        if resp.status_code != 200:
            logger.info("Overpass returned %s: %s", resp.status_code, resp.text[:200])
            return None
        return resp.json().get("elements") or []
    except Exception:
        logger.warning("Overpass request failed", exc_info=True)
        return None


def _clause(filters: list[tuple[str, str | None]], lat: float, lng: float, r_m: float) -> str:
    """Build the nwr selectors for one set of tag filters within a radius."""
    parts = []
    for key, val in filters:
        sel = f'["{key}"="{val}"]' if val is not None else f'["{key}"]'
        parts.append(f"nwr{sel}(around:{r_m:.0f},{lat},{lng});")
    return "".join(parts)


def _coords(el: dict) -> tuple[float, float] | None:
    if "lat" in el and "lon" in el:
        return float(el["lat"]), float(el["lon"])
    center = el.get("center")
    if center and "lat" in center and "lon" in center:
        return float(center["lat"]), float(center["lon"])
    return None


def _fmt_address(tags: dict) -> str:
    num = tags.get("addr:housenumber", "")
    street = tags.get("addr:street", "")
    city = tags.get("addr:city", "")
    line1 = f"{num} {street}".strip()
    parts = [p for p in (line1, city) if p]
    return ", ".join(parts)


def _classify(tags: dict) -> VenueDef | None:
    """Return the first venue definition whose filters match these OSM tags."""
    for venue in SCOUT_VENUES:
        for key, val in venue.filters:
            if key in tags and (val is None or tags[key] == val):
                return venue
    return None


def haversine_mi(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Great-circle distance between two lat/lng points, in miles."""
    r = 3958.8  # earth radius, miles
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dphi = math.radians(b_lat - a_lat)
    dlmb = math.radians(b_lng - a_lng)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _context_query(lat: float, lng: float, r_m: float) -> str:
    """Food/convenience + vending POIs within r_m (radius already includes the
    3-mile food buffer)."""
    food = (
        f'nwr["amenity"~"restaurant|fast_food|cafe"](around:{r_m:.0f},{lat},{lng});'
        f'nwr["shop"~"convenience|supermarket"](around:{r_m:.0f},{lat},{lng});'
    )
    vending = f'nwr["amenity"="vending_machine"](around:{r_m:.0f},{lat},{lng});'
    return f"[out:json][timeout:25];({food}{vending});out center;"


def _business_query(lat: float, lng: float, r_m: float, venues: list[VenueDef]) -> str:
    clauses = "".join(_clause(v.filters, lat, lng, r_m) for v in venues)
    return f"[out:json][timeout:25];({clauses});out center tags;"


def _score(
    venue: VenueDef, nearest_food_mi: float | None, has_vending: bool
) -> tuple[int, list[str]]:
    """Blend type-fit + food-desert + vending into a 0-100 score and reason tags."""
    tags = [venue.label]
    # Food-desert component: distance up to 3 mi maps to 0..100. No food found in
    # the buffer at all is treated as the maximum (best) signal.
    if nearest_food_mi is None:
        food_score = 100.0
        tags.append("No food/store nearby")
    else:
        food_score = min(nearest_food_mi / _FOOD_BUFFER_MI, 1.0) * 100.0
        if nearest_food_mi >= _FOOD_BUFFER_MI:
            tags.append(f"No food within {_FOOD_BUFFER_MI:.0f} mi")
    composite = 0.5 * venue.fit + 0.5 * food_score
    if has_vending:
        composite *= 0.5  # already served — lower priority
        tags.append("Vending nearby (lower priority)")
    return max(0, min(100, round(composite))), tags


# ── Public entry point ──────────────────────────────────────────────────────
def scout(
    *,
    lat: float,
    lng: float,
    radius_mi: float,
    venue_labels: list[str] | None = None,
) -> tuple[list[ScoutBiz], str]:
    """Find + score candidate businesses around ``(lat, lng)``.

    Returns ``(items, reason)``; ``reason`` is "" on success or "unavailable" when
    Overpass could not be reached. Never raises.
    """
    radius_mi = max(0.25, min(float(radius_mi or DEFAULT_RADIUS_MI), _MAX_RADIUS_MI))
    r_m = radius_mi * _MILE_M

    wanted = [v for v in SCOUT_VENUES if not venue_labels or v.label in venue_labels]
    if not wanted:
        wanted = SCOUT_VENUES

    elements = _overpass(_business_query(lat, lng, r_m, wanted))
    if elements is None:
        return [], "unavailable"

    # Context POIs cover radius + 3 mi so every in-radius business has full coverage.
    ctx = _overpass(_context_query(lat, lng, r_m + _FOOD_BUFFER_MI * _MILE_M)) or []
    food_pts: list[tuple[float, float]] = []
    vending_pts: list[tuple[float, float]] = []
    for el in ctx:
        pt = _coords(el)
        if not pt:
            continue
        if (el.get("tags") or {}).get("amenity") == "vending_machine":
            vending_pts.append(pt)
        else:
            food_pts.append(pt)

    items: list[ScoutBiz] = []
    seen: set[str] = set()
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue  # unnamed POIs aren't actionable prospects
        pt = _coords(el)
        if not pt:
            continue
        osm_id = f"{el.get('type')}/{el.get('id')}"
        if osm_id in seen:
            continue
        seen.add(osm_id)
        venue = _classify(tags)
        if not venue:
            continue
        b_lat, b_lng = pt

        nearest_food = min(
            (haversine_mi(b_lat, b_lng, f_lat, f_lng) for f_lat, f_lng in food_pts),
            default=None,
        )
        has_vending = any(
            haversine_mi(b_lat, b_lng, v_lat, v_lng) * _MILE_M <= _VENDING_NEAR_M
            for v_lat, v_lng in vending_pts
        )
        score, fit_tags = _score(venue, nearest_food, has_vending)
        items.append(
            ScoutBiz(
                osm_id=osm_id,
                name=name,
                venue_type=venue.label,
                lat=b_lat,
                lng=b_lng,
                address=_fmt_address(tags),
                phone=tags.get("phone") or tags.get("contact:phone") or "",
                website=tags.get("website") or tags.get("contact:website") or "",
                has_vending_guess="likely yes" if has_vending else "unknown",
                nearest_food_mi=round(nearest_food, 2) if nearest_food is not None else None,
                opportunity_score=score,
                fit_tags=fit_tags,
            )
        )

    items.sort(key=lambda b: b.opportunity_score, reverse=True)
    return items[:_MAX_RESULTS], ""


# ── Optional Google Places enrichment (drill-down) ──────────────────────────
def enrich_place(name: str, lat: float, lng: float) -> dict:
    """Best-effort richer contact data for one business via Google Places.

    Returns a dict of any of ``phone``/``website``/``hours``/``rating`` that were
    found, or ``{}`` when no key is configured or the lookup fails. Never raises.
    """
    if not settings.google_places_api_key:
        return {}
    try:
        # Text Search (New) to find the place, with a tight location bias, then read
        # the contact fields off the first match via a field mask (keeps cost down).
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                "https://places.googleapis.com/v1/places:searchText",
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": settings.google_places_api_key,
                    "X-Goog-FieldMask": (
                        "places.internationalPhoneNumber,places.websiteUri,"
                        "places.rating,places.regularOpeningHours.weekdayDescriptions"
                    ),
                },
                json={
                    "textQuery": name,
                    "locationBias": {
                        "circle": {
                            "center": {"latitude": lat, "longitude": lng},
                            "radius": 500.0,
                        }
                    },
                    "maxResultCount": 1,
                },
            )
        if resp.status_code != 200:
            logger.info("Google Places returned %s: %s", resp.status_code, resp.text[:200])
            return {}
        places = resp.json().get("places") or []
    except Exception:
        logger.warning("Google Places enrich failed for %r", name, exc_info=True)
        return {}
    if not places:
        return {}
    p = places[0]
    out: dict = {}
    if p.get("internationalPhoneNumber"):
        out["phone"] = p["internationalPhoneNumber"]
    if p.get("websiteUri"):
        out["website"] = p["websiteUri"]
    if p.get("rating") is not None:
        out["rating"] = p["rating"]
    hours = (p.get("regularOpeningHours") or {}).get("weekdayDescriptions")
    if hours:
        out["hours"] = hours
    return out
