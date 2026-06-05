"""Orchestrates real-time price fetching across all vendors with AI fallback."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.config import settings
from app.database import engine
from app.models.agent import AgentJob
from app.services import web_search
from app.services.price_fetcher.models import VENDOR_META, FetchError, PriceResult

logger = logging.getLogger(__name__)

_AI_MODEL = "claude-haiku-4-5-20251001"
_MAX_FALLBACK_SEARCHES = 4

# Hard wall-clock budget for a single comparison job. Even when every vendor
# fetch and AI fallback stalls (e.g. a local network/SSL block that never
# completes the TLS handshake), the job stops dispatching once this is exceeded
# and finishes with whatever it has. Keeps the UI from spinning indefinitely
# and bounds the per-SKU cost of a bulk-source run.
_JOB_BUDGET_SECONDS = 25

# Vendor site domains for targeted URL searches. Vendors Supply was archived
# in Inventory v2 (selectors stale, no live scraping); historical AgentJob rows
# may still reference it via VENDOR_META, but it's no longer dispatched.
_VENDOR_SITE = {
    "webstaurantstore": "webstaurantstore.com",
    "candy_machines": "candymachines.com",
}

# Vendors that have editable account config in the settings panel. Sam's Club
# keeps its Club ID / ZIP here for reference, but Sam's is NOT in
# COMPARATOR_FETCH_KEYS: the Render-side BFF fetch is permanently Akamai-blocked
# from a datacenter IP, so member prices only ever arrive by pasting the
# operator's own purchase history (Inventory → "Paste Sam's purchases").
VENDOR_KEYS = ["sams_club", "webstaurantstore", "candy_machines"]

# Vendors the live comparator actually dispatches (direct fetch + AI fallback)
# and renders as checkboxes. Walmart was removed entirely (unreliable scrape,
# prices not worth it); Sam's costs come from the paste-import path instead.
COMPARATOR_FETCH_KEYS = ["webstaurantstore", "candy_machines"]


def _setting_keys(vendor_key: str) -> dict[str, str]:
    """Return AppSetting key names for a vendor's stored config."""
    return {
        "sams_club": {
            "zip": "compare_sams_zip",
            "club_id": "compare_sams_club_id",
            "club_name": "compare_sams_club_name",
        },
        "webstaurantstore": {"email": "compare_webstaurantstore_email"},
        "candy_machines": {"email": "compare_candy_machines_email"},
    }.get(vendor_key, {})


def load_vendor_settings(db: Session) -> dict[str, dict[str, str]]:
    """Load all vendor config from AppSetting table."""
    from app.models.settings import AppSetting

    all_settings = {row.key: row.value for row in db.query(AppSetting).all()}

    result: dict[str, dict[str, str]] = {}
    for vk in VENDOR_KEYS:
        keys = _setting_keys(vk)
        result[vk] = {
            field: all_settings.get(setting_key, "") for field, setting_key in keys.items()
        }
    return result


def save_vendor_setting(db: Session, setting_key: str, value: str) -> None:
    from app.models.settings import AppSetting

    row = db.get(AppSetting, setting_key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=setting_key, value=value))
    db.commit()


def _fetch_webstaurantstore(query: str, vend_cfg: dict) -> list[PriceResult]:
    from app.services.price_fetcher import webstaurantstore

    return webstaurantstore.search_products(query, account_email=vend_cfg.get("email"))


def _fetch_candy_machines(query: str, vend_cfg: dict) -> list[PriceResult]:
    from app.services.price_fetcher import candy_machines

    return candy_machines.search_products(query, account_email=vend_cfg.get("email"))


_FETCHERS = {
    "webstaurantstore": _fetch_webstaurantstore,
    "candy_machines": _fetch_candy_machines,
}


def _ai_fallback(
    query: str,
    vendor_key: str,
    provider: str,
    log: list[dict],
    vendor_cfg: dict | None = None,
) -> list[PriceResult]:
    """Use Claude + web search to find prices when direct fetching fails."""
    if not settings.anthropic_api_key:
        return []

    import anthropic  # type: ignore[import-untyped]

    meta = VENDOR_META.get(vendor_key, {})
    vendor_name = meta.get("label", vendor_key)

    # The dispatched vendors (WebstaurantStore, CandyMachines) are nationwide
    # online sellers, so there's no store/club to scope the AI fallback to.
    location_hint = ""

    site_domain = _VENDOR_SITE.get(vendor_key, "")

    # Two-pass search: (1) site-targeted for real product URLs, (2) general for any prices
    all_results: list[dict] = []
    queries_run: list[str] = []

    if site_domain:
        site_q = f"site:{site_domain} {query}"
        queries_run.append(site_q)
        try:
            site_results = web_search.search(site_q, max_results=4, provider=provider)
            all_results.extend(site_results)
        except Exception as exc:
            log.append({"event": "fallback_search_error", "query": site_q, "error": str(exc)})

    price_q = f"{query} price per case bulk wholesale 2025"
    queries_run.append(price_q)
    try:
        price_results = web_search.search(price_q, max_results=4, provider=provider)
        all_results.extend(price_results)
    except Exception as exc:
        log.append({"event": "fallback_search_error", "query": price_q, "error": str(exc)})

    if not all_results:
        return []

    log.append(
        {
            "event": "ai_fallback_search",
            "vendor": vendor_key,
            "queries": queries_run,
            "result_count": len(all_results),
        }
    )
    search_text = json.dumps(all_results)

    system = (
        f"You are a pricing assistant for a vending business. "
        f"Search results for '{query}' are provided below. "
        f"Extract product listings specifically from {vendor_name}{location_hint}. "
        f"CRITICAL RULES:\n"
        f"- Only include a price (unit_price or case_price) if it is EXPLICITLY stated as a dollar amount in the search result text.\n"
        f"- Do NOT estimate, guess, or infer prices. If price is not in the text, set it to null.\n"
        f"- DO include the product URL even when price is null — the user can click through.\n"
        f"- Prefer URLs from {site_domain if site_domain else vendor_name}.\n"
        f"Return ONLY a valid JSON array of objects with keys: "
        f"product_name (string), unit_price (number or null), case_price (number or null), "
        f"case_qty (integer or null), url (string or null), notes (string). "
        f"If price is null, set notes to 'Price not in search results — visit URL to confirm'. "
        f"No prose. Start with [ and end with ]."
    )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=_AI_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": f"Search results:\n{search_text}"}],
    )

    raw_text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    log.append({"event": "ai_fallback_response", "vendor": vendor_key, "preview": raw_text[:300]})

    try:
        # Strip markdown code fences if Claude wrapped the response
        clean = re.sub(r"^```[a-z]*\n?", "", raw_text.strip())
        clean = re.sub(r"\n?```$", "", clean)
        idx = clean.find("[")
        end = clean.rfind("]")
        parsed = json.loads(clean[idx : end + 1]) if idx != -1 and end > idx else []
    except Exception:
        logger.exception(
            "AI fallback JSON parse failed for vendor %s, query: %s", vendor_key, query[:80]
        )
        return []

    # When the AI fallback surfaces a price on a vendor we don't have a direct
    # fetcher for, route the row to its own ai_ref_{host} key so the results
    # template can group it under "Discovered via web search" and the save
    # flow spawns a new Supplier from the hostname (origin=
    # "comparator_discovered"). The bound vendor's own domain still passes
    # through with the original vendor_key.
    expected_host = (site_domain or "").lower().removeprefix(".")

    fallback_results = []
    for item in parsed if isinstance(parsed, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or "").strip()
        if not name:
            continue
        unit_p = item.get("unit_price")
        case_p = item.get("case_price")
        has_price = bool(unit_p or case_p)
        item_url = item.get("url") or ""
        row_vendor_key = vendor_key
        row_vendor_name = vendor_name
        row_vendor_type = meta.get("type", "online_wholesale")
        if item_url:
            host = (urlparse(item_url).hostname or "").lower().removeprefix("www.")
            if host and expected_host and not host.endswith(expected_host):
                # Off-domain hit — surface as a vendor candidate.
                row_vendor_key = f"ai_ref_{host}"
                row_vendor_name = host
                row_vendor_type = "online_wholesale"
        fallback_results.append(
            PriceResult(
                vendor_key=row_vendor_key,
                vendor_name=row_vendor_name,
                vendor_type=row_vendor_type,
                product_name=name,
                unit_price=float(unit_p) if unit_p else None,
                case_price=float(case_p) if case_p else None,
                case_qty=item.get("case_qty"),
                url=item_url or None,
                notes=str(
                    item.get("notes")
                    or (
                        "Discovered via web search — verify before ordering"
                        if row_vendor_key.startswith("ai_ref_")
                        else "AI-found price"
                        if has_price
                        else "Visit URL to see current price"
                    )
                ),
                source="ai_search" if not row_vendor_key.startswith("ai_ref_") else "ai_ref",
                confidence="low"
                if row_vendor_key.startswith("ai_ref_")
                else ("medium" if has_price else "low"),
            )
        )
    return fallback_results


def run_price_comparison_job(job_id: int) -> None:
    """Background task: run price comparison across all selected vendors."""
    with Session(engine) as db:
        job = db.get(AgentJob, job_id)
        if not job:
            return

        job.status = "running"
        job.started_at = datetime.now()
        db.commit()

        log: list[dict[str, Any]] = []
        all_results: list[dict] = []
        tokens_used = 0
        start = time.monotonic()
        # Reachability: True once any direct fetcher completes its HTTP request
        # without raising (even with zero products). Stays False when every
        # dispatched vendor throws — the signature of a blocked network — which
        # the UI uses to show an honest "couldn't reach vendors" note instead of
        # a bland "no prices found".
        reachable = False
        dispatched = 0

        try:
            params: dict[str, Any] = json.loads(job.input_params or "{}")
            query: str = params.get("product_query", "")
            selected_vendors: list[str] = params.get("vendors", COMPARATOR_FETCH_KEYS)
            provider: str = params.get("search_provider", "duckduckgo")
            vendor_cfg: dict[str, dict] = params.get("vendor_config", {})
            # Bound Product's case pack lets the dispatcher backfill missing
            # case prices from a retail unit price for fairer cross-vendor
            # comparison. Optional; None when the operator runs an unbound
            # query from the comparator form.
            fallback_pack = params.get("fallback_case_pack")
            try:
                fallback_pack = int(fallback_pack) if fallback_pack else None
            except (TypeError, ValueError):
                fallback_pack = None

            if not query:
                raise ValueError("No product query provided.")

            for vendor_key in selected_vendors:
                if vendor_key not in _FETCHERS:
                    continue

                # Stop dispatching once the wall-clock budget is spent so the
                # job (and each SKU of a bulk-source run) can't hang.
                if time.monotonic() - start > _JOB_BUDGET_SECONDS:
                    log.append({"event": "budget_exhausted", "vendor": vendor_key})
                    break

                dispatched += 1
                log.append({"event": "fetch_start", "vendor": vendor_key})
                fetcher = _FETCHERS[vendor_key]
                cfg = vendor_cfg.get(vendor_key, {})

                try:
                    results = fetcher(query, cfg)
                    reachable = True
                    log.append(
                        {
                            "event": "fetch_done",
                            "vendor": vendor_key,
                            "count": len(results),
                            "source": "direct",
                        }
                    )
                except FetchError as exc:
                    log.append({"event": "fetch_error", "vendor": vendor_key, "error": str(exc)})
                    results = []
                except Exception as exc:
                    log.append(
                        {"event": "fetch_exception", "vendor": vendor_key, "error": str(exc)}
                    )
                    results = []

                # Only spend the (slow) AI fallback when there's budget left.
                if not results and time.monotonic() - start <= _JOB_BUDGET_SECONDS:
                    log.append({"event": "fallback_start", "vendor": vendor_key})
                    results = _ai_fallback(query, vendor_key, provider, log, vendor_cfg=cfg)
                    if results:
                        reachable = True
                    log.append(
                        {"event": "fallback_done", "vendor": vendor_key, "count": len(results)}
                    )
                elif not results:
                    log.append({"event": "fallback_skipped_budget", "vendor": vendor_key})

                # Normalize each row's unit/case math, then drop fully empty
                # rows (no price, no URL — they only ever rendered "$—" and
                # confused the operator). v2.
                kept: list[PriceResult] = []
                for r in results:
                    r.normalize(fallback_case_pack=fallback_pack)
                    if not r.is_empty():
                        kept.append(r)
                if len(kept) < len(results):
                    log.append(
                        {
                            "event": "dropped_empty_rows",
                            "vendor": vendor_key,
                            "dropped": len(results) - len(kept),
                        }
                    )

                all_results.extend(r.to_dict() for r in kept)

                # Commit progress after each vendor so the 3s poll shows live
                # status instead of a frozen spinner. Status stays "running".
                job.agent_log = json.dumps(log)[:20_000]
                db.commit()

            # network_blocked = we dispatched at least one direct fetcher and
            # every one threw (no successful HTTP). The single-SKU result view
            # uses this to explain the empty result honestly.
            network_blocked = dispatched > 0 and not reachable
            log.append(
                {
                    "event": "summary",
                    "reachable": reachable,
                    "network_blocked": network_blocked,
                    "result_count": len(all_results),
                }
            )

            job.draft_body = json.dumps(all_results)
            job.prospects_found = len(all_results)
            job.tokens_used = tokens_used
            job.agent_log = json.dumps(log)[:20_000]
            job.status = "done"

        except Exception as exc:
            logger.exception("Price comparison job %d failed", job_id)
            job.status = "error"
            job.error_message = str(exc)
            job.agent_log = json.dumps(log)[:20_000]

        job.finished_at = datetime.now()
        db.commit()


def job_summary(job: AgentJob | None) -> dict[str, Any]:
    """Return the trailing ``summary`` event from a job's agent_log (or {}).

    Lets the result views answer "did we actually reach any vendor, or was the
    network blocked?" without re-deriving it from raw log events.
    """
    if not job or not job.agent_log:
        return {}
    try:
        events = json.loads(job.agent_log)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(events, list):
        return {}
    for ev in reversed(events):
        if isinstance(ev, dict) and ev.get("event") == "summary":
            return ev
    return {}
