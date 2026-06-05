"""Find nearby retail stores using OpenStreetMap Overpass + Nominatim APIs."""

from __future__ import annotations

import json
import re
import ssl
import urllib.parse
import urllib.request
from math import atan2, cos, radians, sin, sqrt

_CTX = ssl._create_unverified_context()
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Map internal brand key → OSM brand tag value
_BRAND_TAGS = {
    "sams_club": "Sam's Club",
}


def geocode_zip(zip_code: str) -> tuple[float, float] | tuple[None, None]:
    url = f"{_NOMINATIM_URL}?postalcode={zip_code.strip()}&country=US&format=json&limit=1"
    req = urllib.request.Request(url, headers={"User-Agent": "PrimeMM/1.0"})
    with urllib.request.urlopen(req, context=_CTX, timeout=10) as resp:
        data = json.loads(resp.read().decode())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    return None, None


def _overpass_query(query: str, timeout: int = 30) -> list[dict]:
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(_OVERPASS_URL, data=data, headers={"User-Agent": "PrimeMM/1.0"})
    with urllib.request.urlopen(req, context=_CTX, timeout=timeout + 5) as resp:
        return json.loads(resp.read().decode()).get("elements", [])


def _extract_store_id(website: str, brand: str) -> str:
    # Sam's Club store URLs look like /club/<slug>/<id>.
    m = re.search(r"/club/[^/]+/(\d+)", website)
    return m.group(1) if m else ""


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return earth_radius_km * 2 * atan2(sqrt(a), sqrt(1 - a))


_SKIP_KEYWORDS = ("pharmacy", "garden center", "tire", "auto", "vision", "fuel")


def find_stores(zip_code: str, brand: str, radius_km: int = 100) -> list[dict]:
    """
    Find nearby stores via OpenStreetMap.

    Args:
        zip_code: US ZIP code
        brand: 'sams_club'
        radius_km: Search radius in kilometers

    Returns:
        List of store dicts sorted by distance:
        {id, name, city, state, address, distance_miles, website}
    """
    if brand not in _BRAND_TAGS:
        raise ValueError(f"Unknown brand: {brand}")

    lat, lon = geocode_zip(zip_code)
    if lat is None:
        raise ValueError(f"Could not geocode ZIP {zip_code}")

    brand_tag = _BRAND_TAGS[brand]
    radius_m = radius_km * 1000

    q = (
        f"[out:json][timeout:25];"
        f'(node["brand"="{brand_tag}"](around:{radius_m},{lat},{lon});'
        f'way["brand"="{brand_tag}"](around:{radius_m},{lat},{lon}););'
        f"out center tags;"
    )

    elements = _overpass_query(q, timeout=30)

    stores: list[dict] = []
    seen_ids: set[str] = set()

    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name", brand_tag)

        # Skip pharmacies, auto centers, etc.
        if any(kw in name.lower() for kw in _SKIP_KEYWORDS):
            continue

        website = tags.get("website", "")
        store_id = _extract_store_id(website, brand)
        if not store_id or store_id in seen_ids:
            continue
        seen_ids.add(store_id)

        city = tags.get("addr:city", "")
        state = tags.get("addr:state", "")
        housenumber = tags.get("addr:housenumber", "")
        street = tags.get("addr:street", "")
        address = f"{housenumber} {street}".strip() if housenumber else street

        el_lat = float(el.get("lat") or el.get("center", {}).get("lat") or lat)
        el_lon = float(el.get("lon") or el.get("center", {}).get("lon") or lon)
        dist_km = _haversine(lat, lon, el_lat, el_lon)

        stores.append(
            {
                "id": store_id,
                "name": name,
                "city": city,
                "state": state,
                "address": address,
                "website": website,
                "distance_km": round(dist_km, 1),
                "distance_miles": round(dist_km * 0.621371, 1),
            }
        )

    stores.sort(key=lambda s: s["distance_km"])
    return stores
