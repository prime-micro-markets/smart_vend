"""Fetch equipment product data from the VendGuys Shopify store via JSON API."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_CATALOG_URL = "https://vendguys.com/collections/machines/products.json?limit=250"
_CANTALOUPE_URL = (
    "https://store.cantaloupe.com/collections/coolers-and-freezers/products.json?limit=250"
)
_SSL_VERIFY = False  # Windows Python can't verify Shopify's intermediate CA via certifi
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class VGProduct:
    title: str
    handle: str
    page_url: str
    image_url: str | None
    price: float | None
    body_text: str  # spec content, HTML stripped


def _shopify_products(url: str, base_product_url: str) -> list[VGProduct]:
    """Fetch a Shopify products.json endpoint and return VGProduct list."""
    with httpx.Client(timeout=15, headers={"User-Agent": _UA}, verify=_SSL_VERIFY) as client:
        resp = client.get(url)
        resp.raise_for_status()
        products_raw: list[dict[str, Any]] = resp.json().get("products", [])

    result: list[VGProduct] = []
    for p in products_raw:
        images = p.get("images") or []
        image_url: str | None = images[0]["src"] if images else None

        variants = p.get("variants") or []
        price: float | None = float(variants[0]["price"]) if variants else None

        body_html: str = p.get("body_html") or ""
        body_text = _strip_html(body_html)

        handle = p.get("handle", "")
        result.append(
            VGProduct(
                title=p.get("title", ""),
                handle=handle,
                page_url=f"{base_product_url}{handle}",
                image_url=image_url,
                price=price,
                body_text=body_text,
            )
        )

    return result


def fetch_catalog() -> list[VGProduct]:
    """Return all products from vendguys.com/collections/machines via Shopify JSON API."""
    return _shopify_products(_CATALOG_URL, "https://vendguys.com/products/")


def fetch_cantaloupe_catalog() -> list[VGProduct]:
    """Return smart cooler products from store.cantaloupe.com via Shopify JSON API."""
    return _shopify_products(_CANTALOUPE_URL, "https://store.cantaloupe.com/products/")


def scrape_url(url: str) -> tuple[str, str | None]:
    """Fetch a product page URL. Returns (text_content, og_image_url_or_None)."""
    try:
        with httpx.Client(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": _UA},
            verify=_SSL_VERIFY,
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception:
        logger.exception("Failed to fetch URL for scraping: %s", url[:100])
        return "", None

    # og:image (most reliable cross-site image signal)
    og_match = re.search(
        r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        html,
        re.IGNORECASE,
    )
    if not og_match:
        og_match = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
            html,
            re.IGNORECASE,
        )
    og_image = og_match.group(1).strip() if og_match else None

    # Fallback: first non-UI img[src] with an image extension
    if not og_image:
        skip = {"logo", "icon", "flag", "banner", "footer", "nav", "avatar", "sprite", "badge"}
        for img_url in re.findall(
            r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|webp)(?:\?[^"\']*)?)["\']',
            html,
            re.IGNORECASE,
        ):
            low = img_url.lower()
            if not any(s in low for s in skip):
                og_image = img_url.strip()
                break

    text = _strip_html(html)
    return text[:6000], og_image


def _strip_html(html: str) -> str:
    """Remove tags, decode common entities, collapse whitespace."""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#?[a-z0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()
