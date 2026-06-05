"""Claude-powered background job for AI supplier sourcing research."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.database import engine
from app.models.agent import AgentJob
from app.models.inventory import Supplier
from app.services import app_settings, web_search

logger = logging.getLogger(__name__)

# Model and tool-call limits are runtime-configurable via the AppSetting
# table (see app.services.app_settings.DEFAULTS for fallback values).
_MAX_LOG_CHARS = 20_000

PRODUCT_CATEGORY_OPTIONS = [
    "snacks (chips, crackers, pretzels)",
    "candy & confections",
    "fresh food (sandwiches, wraps, salads)",
    "beverages (water, juice, soft drinks)",
    "energy & sports drinks",
    "coffee & hot beverages",
    "healthy snacks (protein bars, nuts)",
    "frozen & refrigerated items",
    "personal care (gum, mints)",
]

_TOOL_WEB_SEARCH: dict[str, Any] = {
    "name": "web_search",
    "description": (
        "Search the web for wholesale distributors, food-service suppliers, cash-and-carry "
        "retailers, and vending distributors that sell products to small operators. "
        "Use specific queries like 'wholesale snack distributors Panama City FL', "
        "'vending supplier chips beverages Florida', 'food service distributor micro market'. "
        "Call this multiple times with varied queries to find more options."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query string"},
        },
        "required": ["query"],
    },
}


def _build_system_prompt(
    categories: list[str], location: str, search_focus: str, max_results: int
) -> str:
    cats_str = ", ".join(categories) if categories else "snacks, beverages, fresh food"
    focus_line = f"\nAdditional focus: {search_focus}" if search_focus else ""
    return (
        f"You are a sourcing assistant for Prime Vending, a small veteran-owned smart cooler "
        f"vending and micro-market company based in {location}.\n\n"
        f"Your goal: build a list of up to {max_results} suppliers for these product categories: "
        f"{cats_str}.{focus_line}\n\n"
        f"IMPORTANT: Use BOTH your built-in knowledge AND web searches.\n"
        f"- You already know about major distributors (Sysco, US Foods, Performance Food Group, "
        f"Sam's Club, Costco, BJ's Wholesale, WebstaurantStore, etc.). Include them.\n"
        f"- Use web_search to find regional distributors, local restaurant supply stores, or "
        f"vending-specific distributors serving {location}.\n"
        f"- Include a supplier even if you only know its name and website. "
        f"Leave any unknown fields as empty string — do NOT skip a supplier just because "
        f"you lack a phone number or contact name.\n\n"
        f"After a few searches, stop searching and output your list. "
        f"Do not keep searching if you already have {max_results} suppliers.\n\n"
        f"Output ONLY a valid JSON array — no prose before or after it. "
        f"Use EXACTLY this format (empty string for any unknown field):\n\n"
        f"[\n"
        f"  {{\n"
        f'    "supplier_name": "Sam\'s Club",\n'
        f'    "supplier_type": "cash & carry",\n'
        f'    "categories_served": "snacks, beverages, candy, fresh food",\n'
        f'    "contact_name": "",\n'
        f'    "contact_phone": "",\n'
        f'    "website": "https://www.samsclub.com",\n'
        f'    "pricing_notes": "membership required, bulk pricing",\n'
        f'    "delivery_notes": "in-store pickup or delivery with Plus membership",\n'
        f'    "notes": "nearest location: Panama City"\n'
        f"  }}\n"
        f"]\n\n"
        f"Key names must be exactly: supplier_name, supplier_type, categories_served, "
        f"contact_name, contact_phone, website, pricing_notes, delivery_notes, notes. "
        f"No other text before or after the JSON array."
    )


_NAME_FALLBACKS = ("supplier_name", "name", "company_name", "company", "supplier")


def _extract_json_suppliers(text: str) -> list[dict[str, Any]]:
    from app.services.json_extract import extract_json_list

    data = extract_json_list(text, context="supplier sourcing")

    results = []
    for d in data:
        if not isinstance(d, dict):
            continue
        # Accept whatever key Claude used for the supplier name
        name = next((str(d.get(k, "")).strip() for k in _NAME_FALLBACKS if d.get(k)), "")
        if not name:
            continue
        d["supplier_name"] = name
        results.append(d)
    return results


def run_inventory_search_job(job_id: int) -> None:
    """Background task: run the inventory supplier sourcing agent."""
    with Session(engine) as db:
        job = db.get(AgentJob, job_id)
        if not job:
            return

        job.status = "running"
        job.started_at = datetime.now()
        db.commit()

        found_suppliers: list[dict[str, Any]] = []

        try:
            if not settings.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not configured in .env")

            import anthropic  # type: ignore[import-untyped]

            params: dict[str, Any] = json.loads(job.input_params or "{}")
            categories: list[str] = params.get("product_categories", [])
            location: str = params.get("location", "Panama City, FL")
            search_focus: str = params.get("search_focus", "")
            max_results: int = int(params.get("max_results", 15))
            provider: str = params.get("search_provider", "duckduckgo")

            inventory_model = app_settings.get_str(db, "inventory_model")
            max_tool_calls = app_settings.get_int(
                db, "inventory_max_tool_calls", minimum=1, maximum=30
            )

            system_prompt = _build_system_prompt(categories, location, search_focus, max_results)

            _COMPILE_INSTRUCTION = (
                "Search limit reached. Now output your final supplier list.\n"
                "CRITICAL: You MUST include suppliers from your own training knowledge — "
                "Sysco, US Foods, Performance Food Group, Sam's Club, Costco, "
                "WebstaurantStore, Restaurant Depot, BJ's Wholesale, and any regional "
                f"distributors you know serve {location}. "
                "Do NOT output an empty array. Even if web searches found nothing useful, "
                "you already know these distributors exist — include them.\n"
                "Output ONLY a valid JSON array. No prose, no markdown fences. "
                "Use exactly these key names: supplier_name, supplier_type, categories_served, "
                "contact_name, contact_phone, website, pricing_notes, delivery_notes, notes. "
                "Use empty string for any unknown field. Start your response with [ and end with ]."
            )

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            messages: list[dict[str, Any]] = [
                {"role": "user", "content": "Begin your supplier research now."}
            ]
            log_entries: list[dict[str, Any]] = []
            tool_call_count = 0
            total_tokens = 0
            needs_compile = False

            while True:
                raw = client.messages.with_raw_response.create(
                    model=inventory_model,
                    max_tokens=4096,
                    system=[
                        {
                            "type": "text",
                            "text": system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    tools=[_TOOL_WEB_SEARCH],
                    messages=messages,
                )
                response = raw.parse()
                total_tokens += response.usage.input_tokens + response.usage.output_tokens

                if tool_call_count == 0:
                    try:
                        rl_remaining = raw.headers.get("anthropic-ratelimit-tokens-remaining")
                        rl_reset = raw.headers.get("anthropic-ratelimit-tokens-reset")
                        job.ratelimit_tokens_remaining = int(rl_remaining) if rl_remaining else None
                        job.ratelimit_tokens_reset = rl_reset
                    except Exception:
                        logger.debug(
                            "Failed to parse rate-limit headers for inventory job %d", job_id
                        )

                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "end_turn":
                    final_text = "".join(
                        block.text for block in response.content if hasattr(block, "text")
                    )
                    found_suppliers = _extract_json_suppliers(final_text)
                    log_entries.append(
                        {
                            "event": "end_turn",
                            "suppliers_parsed": len(found_suppliers),
                            "response_preview": final_text[:400],
                        }
                    )
                    break

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use" and block.name == "web_search":
                        tool_call_count += 1
                        query = block.input.get("query", "")
                        log_entries.append(
                            {"event": "tool_call", "query": query, "n": tool_call_count}
                        )
                        try:
                            results = web_search.search(query, max_results=4, provider=provider)
                            result_text = json.dumps(results)
                        except Exception as search_exc:
                            result_text = f"Search error: {search_exc}"
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_text,
                            }
                        )

                if not tool_results:
                    log_entries.append({"event": "no_tool_results_break"})
                    break

                if tool_call_count >= max_tool_calls:
                    # Embed the compile instruction in the SAME user turn as the tool results
                    # so Claude receives search data + instruction in one message (no consecutive
                    # user turns, which causes the API to drop context).
                    log_entries.append({"event": "max_tool_calls_reached"})
                    messages.append(
                        {
                            "role": "user",
                            "content": tool_results
                            + [{"type": "text", "text": _COMPILE_INSTRUCTION}],
                        }
                    )
                    needs_compile = True
                    break

                messages.append({"role": "user", "content": tool_results})

            if needs_compile:
                # Compile call: no tools so Claude must output text.
                # The last user message already contains the compile instruction.
                compile_raw = client.messages.with_raw_response.create(
                    model=inventory_model,
                    max_tokens=4096,
                    system=[
                        {
                            "type": "text",
                            "text": system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=messages,
                )
                compile_resp = compile_raw.parse()
                total_tokens += compile_resp.usage.input_tokens + compile_resp.usage.output_tokens
                final_text = "".join(
                    block.text for block in compile_resp.content if hasattr(block, "text")
                )
                found_suppliers = _extract_json_suppliers(final_text)
                log_entries.append(
                    {
                        "event": "forced_compile",
                        "suppliers_parsed": len(found_suppliers),
                        "response_preview": final_text[:600],
                    }
                )

            # Persist results JSON for display on job page
            job.draft_body = json.dumps(found_suppliers)

            # Auto-add discovered suppliers (skip duplicates by name, case-insensitive)
            created = skipped = 0
            for s in found_suppliers:
                name = (s.get("supplier_name") or "").strip()
                if not name:
                    continue
                exists = db.query(Supplier).filter(Supplier.name.ilike(name)).first()
                if exists:
                    skipped += 1
                else:
                    cats_served = s.get("categories_served") or ""
                    pricing = s.get("pricing_notes") or ""
                    delivery = s.get("delivery_notes") or ""
                    extra = s.get("notes") or ""
                    notes_parts = []
                    if cats_served:
                        notes_parts.append(f"Categories: {cats_served}")
                    if pricing:
                        notes_parts.append(f"Pricing: {pricing}")
                    if delivery:
                        notes_parts.append(f"Delivery: {delivery}")
                    if extra:
                        notes_parts.append(extra)
                    notes_str = " | ".join(notes_parts) if notes_parts else None

                    supplier = Supplier(
                        name=name,
                        supplier_type=s.get("supplier_type") or None,
                        contact_name=s.get("contact_name") or None,
                        contact_phone=s.get("contact_phone") or None,
                        website=s.get("website") or None,
                        notes=notes_str,
                    )
                    db.add(supplier)
                    created += 1

            job.tokens_used = total_tokens
            job.prospects_found = len(found_suppliers)
            job.prospects_created = created
            job.prospects_skipped = skipped
            job.agent_log = json.dumps(log_entries)[:_MAX_LOG_CHARS]
            job.status = "done"

        except Exception as exc:
            logger.exception("Inventory search job %d failed", job_id)
            job.status = "error"
            job.error_message = str(exc)

        job.finished_at = datetime.now()
        db.commit()
