"""Claude-powered agentic background jobs for lead research and email drafting."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.database import engine
from app.models.agent import AgentJob
from app.models.sales import Prospect
from app.services import app_settings, web_search

logger = logging.getLogger(__name__)

# Model and tool-call limits are runtime-configurable via the AppSetting
# table (see app.services.app_settings.DEFAULTS for fallback values).
_MAX_LOG_CHARS = 20_000


def _make_search_tool(location: str) -> dict[str, Any]:
    return {
        "name": "web_search",
        "description": (
            f"Search the web for local businesses in {location}. "
            f"Use specific queries like 'gyms {location}', 'hotels near {location}', "
            f"'corporate offices {location}'. Call this multiple times with varied queries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
            },
            "required": ["query"],
        },
    }


def _build_research_system_prompt(
    venue_types: list[str],
    location: str,
    search_focus: str,
    max_leads: int,
) -> str:
    types_str = ", ".join(venue_types) if venue_types else "gyms, hotels, corporate offices, condos"
    focus_line = f"\nAdditional focus/instructions: {search_focus}" if search_focus else ""
    return (
        f"You are a lead generation assistant for {settings.company_blurb}\n\n"
        f"Your goal: find up to {max_leads} real businesses in {location} "
        f"that match these venue types: {types_str}.{focus_line}\n\n"
        "Use the web_search tool as many times as needed to find specific businesses.\n"
        "For each business found, extract: company name, street address, city, venue type, "
        "estimated foot traffic (low/medium/high), contact name if listed on their website, "
        "phone number, and website URL.\n\n"
        "IMPORTANT: Do NOT search for email addresses. They are almost never publicly listed "
        "and searching for them wastes searches. Focus on finding the business name, address, "
        "phone, and website — these are reliably findable.\n\n"
        "When you have gathered enough results, output ONLY a valid JSON array. "
        "Each object must have these exact keys (use empty string if unknown):\n"
        "  company_name, address, city, venue_type, foot_traffic_estimate, "
        "contact_name, phone, website, notes\n\n"
        "Do not include any text before or after the JSON array. "
        "Only include businesses with a real company_name."
    )


def _extract_json_leads(text: str) -> list[dict[str, Any]]:
    from app.services.json_extract import extract_json_list

    data = extract_json_list(text, context="lead research")
    return [d for d in data if d.get("company_name")]


def _build_template_email(prospect: Prospect) -> tuple[str, str]:
    """Return (subject, body) template draft — no AI, generated from prospect data."""
    booking_url = settings.google_booking_url
    scheduling = (
        f"Feel free to grab a time that works here: {booking_url}"
        if booking_url
        else "Just reply and we can find a time that works."
    )
    subject = f"Modernize {prospect.company_name}'s Amenities — Smart Cooler Partnership"
    name_line = f"Hi {prospect.contact_name}," if prospect.contact_name else "Hi,"
    body = (
        f"{name_line}\n\n"
        f"I'm reaching out from Prime Micro Markets, a veteran-owned business based in "
        f"{prospect.city}. "
        f"We partner with high-traffic locations like {prospect.company_name} to place our "
        f"state-of-the-art "
        f"smart cooler units at no cost — no equipment fees, no stocking hassle, no risk to "
        f"you.\n\n"
        f"Our units are fully cashless, touchscreen-enabled, and remotely monitored in real time. "
        f"We handle all restocking and maintenance, and you get a premium amenity your customers "
        f"will love "
        f"without lifting a finger. We're also flexible on product selection — we'll stock what "
        f"works best "
        f"for your clientele.\n\n"
        f"Given the foot traffic at {prospect.company_name}, I think we'd be a great fit. "
        f"Would you have 10 minutes for a quick call to see if it makes sense?\n\n"
        f"{scheduling}\n\n"
        f"Best,\n[Your Name]\nPrime Micro Markets"
    )
    return subject, body


def run_research_job(job_id: int) -> None:
    """Background task: run the research agent to find prospects."""
    with Session(engine) as db:
        job = db.get(AgentJob, job_id)
        if not job:
            return

        job.status = "running"
        job.started_at = datetime.now()
        db.commit()

        leads: list[dict[str, Any]] = []

        try:
            if not settings.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not configured in .env")

            import anthropic  # type: ignore[import-untyped]

            params: dict[str, Any] = json.loads(job.input_params or "{}")
            venue_types: list[str] = params.get("venue_types", [])
            location: str = params.get("location", "Panama City, FL")
            search_focus: str = params.get("search_focus", "")
            max_leads: int = int(params.get("max_leads", 20))
            provider: str = params.get("search_provider", "duckduckgo")

            research_model = app_settings.get_str(db, "research_model")
            max_tool_calls = app_settings.get_int(
                db, "research_max_tool_calls", minimum=1, maximum=50
            )

            system_prompt = _build_research_system_prompt(
                venue_types=venue_types,
                location=location,
                search_focus=search_focus,
                max_leads=max_leads,
            )
            search_tool = _make_search_tool(location)

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            messages: list[dict[str, Any]] = [
                {"role": "user", "content": "Begin your research now and find businesses."}
            ]
            log_entries: list[dict[str, Any]] = []
            tool_call_count = 0
            total_tokens = 0

            while tool_call_count <= max_tool_calls:
                raw = client.messages.with_raw_response.create(
                    model=research_model,
                    max_tokens=2048,
                    system=[
                        {
                            "type": "text",
                            "text": system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    tools=[search_tool],
                    messages=messages,
                )
                response = raw.parse()
                total_tokens += response.usage.input_tokens + response.usage.output_tokens

                # Capture rate-limit snapshot from first response
                if tool_call_count == 0:
                    try:
                        rl_remaining = raw.headers.get("anthropic-ratelimit-tokens-remaining")
                        rl_reset = raw.headers.get("anthropic-ratelimit-tokens-reset")
                        job.ratelimit_tokens_remaining = int(rl_remaining) if rl_remaining else None
                        job.ratelimit_tokens_reset = rl_reset
                    except Exception:
                        logger.debug("Failed to parse rate-limit headers for job %d", job_id)

                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "end_turn":
                    final_text = "".join(
                        block.text for block in response.content if hasattr(block, "text")
                    )
                    leads = _extract_json_leads(final_text)
                    log_entries.append(
                        {
                            "event": "end_turn",
                            "leads_parsed": len(leads),
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
                            results = web_search.search(query, max_results=3, provider=provider)
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
                    # Response had no tool calls and wasn't end_turn — force compilation
                    log_entries.append({"event": "no_tool_results_break"})
                    break
                messages.append({"role": "user", "content": tool_results})
            else:
                # Tool call limit reached — ask Claude to compile what it already found
                log_entries.append({"event": "max_tool_calls_reached"})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You have reached the search limit. "
                            "Output ONLY the JSON array of every business you found so far. "
                            "Include any business you identified even if details are incomplete — "
                            "use empty string for unknown fields. No other text, just the JSON "
                            "array."
                        ),
                    }
                )
                compile_raw = client.messages.with_raw_response.create(
                    model=research_model,
                    max_tokens=2048,
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
                leads = _extract_json_leads(final_text)
                log_entries.append(
                    {
                        "event": "forced_compile",
                        "leads_parsed": len(leads),
                        "response_preview": final_text[:400],
                    }
                )

            created = skipped = 0
            for lead in leads:
                name = (lead.get("company_name") or "").strip()
                city = (lead.get("city") or "Panama City").strip()
                if not name:
                    continue
                exists = (
                    db.query(Prospect)
                    .filter(Prospect.company_name == name, Prospect.city == city)
                    .first()
                )
                if exists:
                    skipped += 1
                else:
                    p = Prospect(
                        company_name=name,
                        address=lead.get("address") or None,
                        city=city,
                        venue_type=lead.get("venue_type") or None,
                        foot_traffic_estimate=lead.get("foot_traffic_estimate") or None,
                        contact_name=lead.get("contact_name") or None,
                        contact_phone=lead.get("phone") or None,
                        website=lead.get("website") or None,
                        notes=lead.get("notes") or None,
                        source="agent",
                        pipeline_stage="lead",
                        source_job_id=job.id,
                    )
                    p.template_draft_subject, p.template_draft_body = _build_template_email(p)
                    db.add(p)
                    created += 1

            job.tokens_used = total_tokens
            job.prospects_found = len(leads)
            job.prospects_created = created
            job.prospects_skipped = skipped
            job.agent_log = json.dumps(log_entries)[:_MAX_LOG_CHARS]
            job.status = "done"

        except Exception as exc:
            logger.exception("Research job %d failed", job_id)
            job.status = "error"
            job.error_message = str(exc)

        job.finished_at = datetime.now()
        db.commit()


def _build_email_draft_prompt(prospect: Prospect) -> str:
    booking_url = settings.google_booking_url
    scheduling_note = (
        f"Include this scheduling link for them to book a quick call: {booking_url}"
        if booking_url
        else "Suggest they reply to schedule a brief call or site visit."
    )
    phone_line = f"\n  Phone: {prospect.contact_phone}" if prospect.contact_phone else ""
    website_line = f"\n  Website: {prospect.website}" if prospect.website else ""
    return (
        f"You are writing a cold outreach email on behalf of Prime Micro Markets.\n\n"
        f"About us: {settings.company_blurb}\n\n"
        f"Key selling points to weave in naturally (don't list them robotically — work them into "
        f"the narrative):\n"
        f"- Zero cost to the host location: no equipment purchase, no installation fees, no "
        f"stocking burden\n"
        f"- State-of-the-art units: touchscreen interfaces, fully cashless (tap/card/mobile pay), "
        f"remote monitoring\n"
        f"- We handle everything: restocking, maintenance, and inventory — the host does nothing\n"
        f"- Product flexibility: we customize the product mix to fit the venue's clientele\n"
        f"- Premium customer experience: modern, attractive machines that enhance the location's "
        f"brand\n\n"
        f"Prospect details:\n"
        f"  Business: {prospect.company_name}\n"
        f"  Contact: {prospect.contact_name or 'the owner/manager'}\n"
        f"  Title: {prospect.contact_title or ''}\n"
        f"  Venue type: {prospect.venue_type or 'business'}\n"
        f"  City: {prospect.city}{phone_line}{website_line}\n"
        f"  Notes: {prospect.notes or ''}\n\n"
        f"Write a short, compelling cold outreach email (3–4 paragraphs max). "
        f"Make the subject line intriguing and venue-specific — avoid generic phrases. "
        f"The tone should be confident and professional, but warm and human — not salesy or "
        f"hype-driven. "
        f"Focus on what's in it for them: a cutting-edge amenity added to their location at zero "
        f"cost or effort. "
        f"Do NOT mention revenue sharing or passive income — the value prop is the free, premium "
        f"amenity and customer experience. "
        f"{scheduling_note}\n\n"
        f"Format your response exactly as:\n"
        f"Subject: <subject line>\n\n"
        f"<email body>"
    )


def run_email_draft_job(job_id: int) -> None:
    """Background task: draft a personalized outreach email for a prospect."""
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

            prospect = db.get(Prospect, job.prospect_id)
            if not prospect:
                raise RuntimeError(f"Prospect {job.prospect_id} not found")

            import anthropic  # type: ignore[import-untyped]

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            raw = client.messages.with_raw_response.create(
                model=app_settings.get_str(db, "email_model"),
                max_tokens=1024,
                messages=[{"role": "user", "content": _build_email_draft_prompt(prospect)}],
            )
            response = raw.parse()
            job.tokens_used = response.usage.input_tokens + response.usage.output_tokens

            try:
                rl_remaining = raw.headers.get("anthropic-ratelimit-tokens-remaining")
                rl_reset = raw.headers.get("anthropic-ratelimit-tokens-reset")
                job.ratelimit_tokens_remaining = int(rl_remaining) if rl_remaining else None
                job.ratelimit_tokens_reset = rl_reset
            except Exception:
                logger.debug("Failed to parse rate-limit headers for email draft job %d", job_id)

            full_text = response.content[0].text if response.content else ""

            subject = ""
            body = full_text
            subject_match = re.match(r"Subject:\s*(.+?)(\n|$)", full_text)
            if subject_match:
                subject = subject_match.group(1).strip()
                body = full_text[subject_match.end() :].strip()

            job.draft_subject = subject
            job.draft_body = body
            job.status = "done"

        except Exception as exc:
            logger.exception("Email draft job %d failed", job_id)
            job.status = "error"
            job.error_message = str(exc)

        job.finished_at = datetime.now()
        db.commit()
