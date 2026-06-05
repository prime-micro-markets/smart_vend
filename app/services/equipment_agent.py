"""Background job: refresh equipment specs via direct page scraping + Claude extraction.

Flow per run:
  1. Fetch VendGuys catalog (Shopify JSON API) — authoritative source for matched units.
  2. Match each DB unit to a VendGuys product where possible (model-number + name tokens).
  3. Build a content context per unit:
       - VendGuys match  → body_text from Shopify JSON + CDN image URL
       - No match        → scrape unit.product_url for text + og:image
  4. Send batches of 4 units to Claude (one message, zero tool calls) for JSON extraction.
  5. For each record: download image locally, apply update, commit.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_SSL_VERIFY = False  # Windows Python can't verify Shopify's intermediate CA via certifi

from app.config import settings
from app.database import engine
from app.models.agent import AgentJob
from app.models.equipment import EquipmentUnit
from app.services import app_settings, firecrawl_client, vendguys_scraper
from app.services.vendguys_scraper import VGProduct, fetch_cantaloupe_catalog

# ── image storage ────────────────────────────────────────────────────────────
_EQUIPMENT_IMG_DIR = Path(__file__).parent.parent / "static" / "images" / "equipment"
_ALLOWED_IMG_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_IMG_MAX_BYTES = 10 * 1024 * 1024
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── agent config ─────────────────────────────────────────────────────────────
# Model and batch size are runtime-configurable via the AppSetting table
# (see app.services.app_settings.DEFAULTS for fallback values).
_MAX_LOG_CHARS = 20_000

_SYSTEM_PROMPT = """\
You are a data extraction assistant for vending equipment specs.

Product page content for one or more equipment units is provided. For each unit (identified
by "=== id=N: ... ==="), extract the specifications from the provided content only.
Use null for any field not explicitly stated in the content.

Return ONLY a valid JSON array — one object per unit — using exactly these keys:

[{
  "id": <integer — as given>,
  "price_low": <integer USD or null>,
  "price_high": <integer USD or null>,
  "price_notes": <string or null>,
  "monthly_fee": <float or null>,
  "processing_fee_pct": <float or null>,
  "warranty_years": <integer or null>,
  "warranty_notes": <string or null>,
  "extended_warranty_available": <true or false>,
  "extended_warranty_notes": <string or null>,
  "height_in": <float inches or null>,
  "width_in": <float inches or null>,
  "depth_in": <float inches or null>,
  "weight_lbs": <float or null>,
  "capacity_cu_ft": <float or null>,
  "capacity_units": <integer or null>,
  "power_watts": <integer or null>,
  "operating_temp_low": <float °F or null>,
  "operating_temp_high": <float °F or null>,
  "connectivity": <string e.g. "4G, WiFi" or null>,
  "payment_types": <string or null>,
  "ai_accuracy_pct": <float or null>,
  "certifications": <string or null>,
  "delivery_days_min": <integer or null>,
  "delivery_days_max": <integer or null>,
  "delivery_notes": <string or null>,
  "highlights": <newline-separated bullet points or null>,
  "image_url": <direct image URL found in content, or null>,
  "product_url": <best product page URL from content or null>
}]

Do not include any text before or after the JSON array."""


# ── VendGuys matching ─────────────────────────────────────────────────────────


def _match_vendguys(unit: EquipmentUnit, catalog: list[VGProduct]) -> VGProduct | None:
    """Return the best-matching VendGuys product for a DB unit, or None."""
    # Always score — don't trust existing product_url (may be stale/wrong from prior run)
    unit_text = f"{unit.manufacturer or ''} {unit.product_name} {unit.product_line or ''}".lower()
    unit_text = re.sub(r"[^a-z0-9\s]", "", unit_text)
    unit_tokens = set(unit_text.split())

    # 3-digit+ model numbers are the strongest signal (360, 542, 550, 1200, …)
    # No \b anchors — "550ct" contains "550" but has no word boundary mid-token
    model_numbers = re.findall(r"\d{3,}", unit_text)

    # Keep noise minimal: only articles/prepositions that carry zero discriminating value
    _NOISE = {
        "the",
        "a",
        "and",
        "or",
        "with",
        "for",
        "of",
        "in",
        "by",
        "vending",
        "machine",
        "store",
        "market",
    }

    best: VGProduct | None = None
    best_score = 0

    for vg in catalog:
        vg_text = re.sub(r"[^a-z0-9\s]", "", vg.title.lower())
        vg_handle = vg.handle.replace("-", " ").replace("%e2%84%a2", "")
        vg_tokens = set(vg_text.split())
        score = 0

        # Model number hit (strong)
        for num in model_numbers:
            if num in vg.handle or num in vg_text:
                score += 10

        # Token overlap (excluding noise)
        score += len((unit_tokens & vg_tokens) - _NOISE)

        # Manufacturer prefix bonus
        mfr = (unit.manufacturer or "").lower().split()[0]
        if mfr and mfr in vg_text:
            score += 2

        if score > best_score and score >= 3:
            best_score = score
            best = vg

    return best


def _match_cantaloupe(unit: EquipmentUnit, catalog: list[VGProduct]) -> VGProduct | None:
    """Return the best-matching Cantaloupe store product for a Cantaloupe DB unit."""
    if (unit.manufacturer or "").lower() != "cantaloupe":
        return None

    unit_text = f"{unit.product_name} {unit.product_line or ''}".lower()
    unit_text = re.sub(r"[^a-z0-9\s]", "", unit_text)
    unit_tokens = set(unit_text.split())
    model_numbers = re.findall(r"\d{3,}", unit_text)

    _NOISE = {"the", "a", "and", "or", "with", "for", "of", "in", "by", "single", "one"}

    best: VGProduct | None = None
    best_score = 0

    for vg in catalog:
        vg_text = re.sub(r"[^a-z0-9\s]", "", vg.title.lower())
        # Strip "[Cantaloupe One]" suffix (free tier variant) to avoid false positives
        vg_text = re.sub(r"cantaloupe one", "", vg_text)
        vg_tokens = set(vg_text.split())
        score = 0

        for num in model_numbers:
            if num in vg.handle or num in vg_text:
                score += 10

        score += len((unit_tokens & vg_tokens) - _NOISE)

        # Penalise the free "Cantaloupe One" bundles — prefer paid listings for spec data
        if vg.price == 0.0:
            score -= 2

        if score > best_score and score >= 2:
            best_score = score
            best = vg

    return best


# ── per-unit context resolution ───────────────────────────────────────────────


def _build_context(
    unit: EquipmentUnit,
    catalog: list[VGProduct],
    cantaloupe_catalog: list[VGProduct],
    log_entries: list[dict],
) -> dict[str, Any]:
    """Return a dict with keys: unit, content, source_image_url, vendguys_page_url, cantaloupe_page_url."""
    # 1. Try VendGuys catalog match
    vg = _match_vendguys(unit, catalog)
    if vg:
        content = f"Product: {vg.title}\nPrice: ${vg.price:.2f}\n\n{vg.body_text[:4000]}"
        log_entries.append(
            {
                "event": "matched_vendguys",
                "unit_id": unit.id,
                "vg_title": vg.title,
                "vg_url": vg.page_url,
            }
        )
        return {
            "unit": unit,
            "content": content,
            "source_image_url": vg.image_url,
            "vendguys_page_url": vg.page_url,
            "cantaloupe_page_url": None,
        }

    # 2. Try Cantaloupe catalog match (Cantaloupe units only)
    ct = _match_cantaloupe(unit, cantaloupe_catalog)
    if ct:
        price_str = f"${ct.price:.2f}" if ct.price else "contact for pricing"
        content = f"Product: {ct.title}\nPrice: {price_str}\n\n{ct.body_text[:4000]}"
        log_entries.append(
            {
                "event": "matched_cantaloupe",
                "unit_id": unit.id,
                "ct_title": ct.title,
                "ct_url": ct.page_url,
            }
        )
        return {
            "unit": unit,
            "content": content,
            "source_image_url": ct.image_url,
            "vendguys_page_url": None,
            "cantaloupe_page_url": ct.page_url,
        }

    # 3. Fallback: scrape existing product_url
    if unit.product_url:
        text, og_image = vendguys_scraper.scrape_url(unit.product_url)
        log_entries.append(
            {
                "event": "scraped_url",
                "unit_id": unit.id,
                "url": unit.product_url[:80],
                "chars": len(text),
                "og_image": bool(og_image),
            }
        )
        return {
            "unit": unit,
            "content": text,
            "source_image_url": og_image,
            "vendguys_page_url": None,
            "cantaloupe_page_url": None,
        }

    log_entries.append({"event": "no_source", "unit_id": unit.id})
    return {
        "unit": unit,
        "content": "",
        "source_image_url": None,
        "vendguys_page_url": None,
        "cantaloupe_page_url": None,
    }


# ── Claude extraction ─────────────────────────────────────────────────────────


def _extract_batch(
    client: Any,
    contexts: list[dict[str, Any]],
    log_entries: list[dict],
    model: str,
) -> tuple[list[dict[str, Any]], int]:
    """Single Claude call — no tool use — to extract specs for up to 4 units.

    Returns (records, tokens_used).
    """
    sections: list[str] = []
    for ctx in contexts:
        unit: EquipmentUnit = ctx["unit"]
        header = f"=== id={unit.id}: {unit.manufacturer} {unit.product_name} ==="
        sections.append(f"{header}\n{ctx['content'] or '(no content available)'}")

    user_message = "\n\n".join(sections)

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    tokens = response.usage.input_tokens + response.usage.output_tokens
    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    records = _extract_json_data(text)

    batch_ids = {ctx["unit"].id for ctx in contexts}
    records = [r for r in records if r.get("id") in batch_ids]
    log_entries.append(
        {
            "event": "batch_extracted",
            "unit_ids": list(batch_ids),
            "records": len(records),
            "tokens": tokens,
        }
    )
    return records, tokens


# ── main job ──────────────────────────────────────────────────────────────────


def run_equipment_refresh_job(job_id: int) -> None:
    """Background task: refresh equipment specs from VendGuys + direct URL scraping."""
    with Session(engine) as db:
        job = db.get(AgentJob, job_id)
        if not job:
            return

        job.status = "running"
        job.started_at = datetime.now()
        db.commit()

        try:
            if not settings.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not configured in .env")

            import anthropic  # type: ignore[import-untyped]

            params: dict[str, Any] = json.loads(job.input_params or "{}")
            if isinstance(params, list):
                unit_ids: list[int] = params
            else:
                unit_ids = params.get("unit_ids", [])

            equipment_model = app_settings.get_str(db, "equipment_model")
            batch_size = app_settings.get_int(db, "equipment_batch_size", minimum=1, maximum=10)

            # Refresh all active units (archived ones are off-catalog and skipped). Pricing is
            # owned by the distributor-sourcing system, so the refresh never writes price fields
            # (see _apply_update) — that's what stops the old price drift. On verified/curated
            # rows (is_locked) the refresh only fills genuinely-empty spec gaps; it never
            # overwrites hand-checked data or downgrades the "verified" badge.
            base_q = db.query(EquipmentUnit).filter(EquipmentUnit.status == "active")
            units = (
                base_q.filter(EquipmentUnit.id.in_(unit_ids)).all() if unit_ids else base_q.all()
            )

            if not units:
                job.status = "done"
                job.agent_log = json.dumps([{"event": "no_units_found"}])
                job.finished_at = datetime.now()
                db.commit()
                return

            locked_in_run = sum(1 for u in units if u.is_locked)
            log_entries: list[dict[str, Any]] = [
                {"event": "start", "units": len(units), "locked_gap_fill_only": locked_in_run}
            ]

            # ── 1. Fetch external catalogs ─────────────────────────────────
            try:
                catalog = vendguys_scraper.fetch_catalog()
                log_entries.append({"event": "vendguys_catalog", "products": len(catalog)})
            except Exception as exc:
                catalog = []
                log_entries.append({"event": "vendguys_catalog_error", "error": str(exc)})

            try:
                cantaloupe_catalog = fetch_cantaloupe_catalog()
                log_entries.append(
                    {"event": "cantaloupe_catalog", "products": len(cantaloupe_catalog)}
                )
            except Exception as exc:
                cantaloupe_catalog = []
                log_entries.append({"event": "cantaloupe_catalog_error", "error": str(exc)})

            # ── 2. Build per-unit content context ─────────────────────────
            contexts = [_build_context(u, catalog, cantaloupe_catalog, log_entries) for u in units]

            # ── 3. Extract specs in batches ────────────────────────────────
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            final_data: list[dict[str, Any]] = []
            total_tokens = 0

            batches = [contexts[i : i + batch_size] for i in range(0, len(contexts), batch_size)]
            for batch_num, batch in enumerate(batches, 1):
                log_entries.append(
                    {
                        "event": "batch_start",
                        "batch": batch_num,
                        "unit_ids": [c["unit"].id for c in batch],
                    }
                )
                try:
                    records, batch_tokens = _extract_batch(
                        client, batch, log_entries, equipment_model
                    )
                    total_tokens += batch_tokens

                    # Inject source_image_url if Claude didn't find one
                    for ctx in batch:
                        uid = ctx["unit"].id
                        rec = next((r for r in records if r.get("id") == uid), None)
                        if rec is None:
                            rec = {"id": uid}
                            records.append(rec)
                        if not rec.get("image_url") and ctx["source_image_url"]:
                            rec["image_url"] = ctx["source_image_url"]
                        # Catalog scoring is authoritative — always trust it over Claude's extraction
                        if ctx.get("vendguys_page_url"):
                            rec["product_url"] = ctx["vendguys_page_url"]
                        elif ctx.get("cantaloupe_page_url"):
                            rec["product_url"] = ctx["cantaloupe_page_url"]

                    final_data.extend(records)
                except Exception as exc:
                    logger.exception(
                        "Equipment refresh batch %d failed for job %d", batch_num, job_id
                    )
                    log_entries.append(
                        {"event": "batch_error", "batch": batch_num, "error": str(exc)}
                    )

            # ── 4. Download images locally ─────────────────────────────────
            for record in final_data:
                raw_url = record.get("image_url")
                if not raw_url or raw_url.startswith("/static/"):
                    continue
                uid = record.get("id")
                existing = db.get(EquipmentUnit, uid) if isinstance(uid, int) else None
                if existing and existing.image_url and existing.image_url.startswith("/static/"):
                    record.pop("image_url", None)
                    log_entries.append({"event": "image_kept_local", "unit_id": uid})
                    continue
                local_path = _download_image(raw_url, uid)
                if local_path:
                    record["image_url"] = local_path
                    log_entries.append({"event": "image_downloaded", "unit_id": uid})
                    continue

                # Fallback: ask Firecrawl for the product page's og:image, which
                # is far more reliable than the brittle regex og:image scrape.
                fc_path = None
                page_url = record.get("product_url") or raw_url
                if page_url and firecrawl_client.is_enabled():
                    og_img = firecrawl_client.scrape_og_image(page_url)
                    if og_img:
                        fc_path = _download_image(og_img, uid)
                if fc_path:
                    record["image_url"] = fc_path
                    log_entries.append({"event": "image_downloaded_firecrawl", "unit_id": uid})
                else:
                    record["image_url"] = None
                    log_entries.append(
                        {"event": "image_download_failed", "unit_id": uid, "url": raw_url[:100]}
                    )

            # ── 5. Write to DB ─────────────────────────────────────────────
            updated = 0
            now = datetime.now()
            for record in final_data:
                uid = record.get("id")
                if not isinstance(uid, int):
                    continue
                unit = db.get(EquipmentUnit, uid)
                if not unit:
                    continue
                _apply_update(unit, record, now, locked=unit.is_locked)
                updated += 1

            db.commit()
            log_entries.append({"event": "done", "units_updated": updated})

            job.tokens_used = total_tokens
            job.agent_log = json.dumps(log_entries)[:_MAX_LOG_CHARS]
            job.prospects_created = updated
            job.status = "done"

        except Exception as exc:
            logger.exception("Equipment refresh job %d failed", job_id)
            job.status = "error"
            job.error_message = str(exc)

        job.finished_at = datetime.now()
        db.commit()


# ── helpers ───────────────────────────────────────────────────────────────────


def _extract_json_data(text: str) -> list[dict[str, Any]]:
    from app.services.json_extract import extract_json_list

    return extract_json_list(text, context="equipment refresh")


def _download_image(url: str, unit_id: int) -> str | None:
    _EQUIPMENT_IMG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(
            timeout=12,
            follow_redirects=True,
            headers={"User-Agent": _BROWSER_UA, "Accept": "image/*,*/*;q=0.8"},
            verify=_SSL_VERIFY,
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").split(";")[0].strip()
            if not ct.startswith("image/"):
                return None
            if len(resp.content) > _IMG_MAX_BYTES:
                return None
            _CT_EXT = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }
            ext = _CT_EXT.get(ct)
            if not ext:
                suffix = Path(url.split("?")[0]).suffix.lower()
                ext = suffix if suffix in _ALLOWED_IMG_SUFFIXES else ".jpg"
            for old in _EQUIPMENT_IMG_DIR.glob(f"{unit_id}.*"):
                old.unlink(missing_ok=True)
            dest = _EQUIPMENT_IMG_DIR / f"{unit_id}{ext}"
            dest.write_bytes(resp.content)
            return f"/static/images/equipment/{unit_id}{ext}"
    except Exception:
        logger.exception("Image download failed for unit %d from %s", unit_id, url[:80])
        return None


# Pricing now lives on EquipmentSource rows (recompute_best_price denormalizes the best one
# onto the unit), so the AI refresh must never write these — that's what caused price drift.
_SOURCE_OWNED_FIELDS = frozenset(
    {"price_low", "price_high", "price_notes", "monthly_fee", "processing_fee_pct"}
)


def _apply_update(
    unit: EquipmentUnit, record: dict[str, Any], now: datetime, locked: bool = False
) -> None:
    fields = [
        "price_low",
        "price_high",
        "price_notes",
        "monthly_fee",
        "processing_fee_pct",
        "warranty_years",
        "warranty_notes",
        "extended_warranty_available",
        "extended_warranty_notes",
        "height_in",
        "width_in",
        "depth_in",
        "weight_lbs",
        "capacity_cu_ft",
        "capacity_units",
        "power_watts",
        "operating_temp_low",
        "operating_temp_high",
        "connectivity",
        "payment_types",
        "ai_accuracy_pct",
        "certifications",
        "delivery_days_min",
        "delivery_days_max",
        "delivery_notes",
        "highlights",
        "image_url",
        "product_url",
    ]
    for field in fields:
        if field in _SOURCE_OWNED_FIELDS:
            continue  # owned by the distributor-sourcing system; never auto-written
        value = record.get(field)
        if value is None:
            continue
        if locked and getattr(unit, field) not in (None, ""):
            continue  # verified row: only fill genuinely-empty gaps, never overwrite curated data
        setattr(unit, field, value)
    unit.last_refreshed = now
    if not locked:
        # Don't downgrade a verified/curated row's confidence badge.
        unit.data_confidence = "ai_refreshed"
