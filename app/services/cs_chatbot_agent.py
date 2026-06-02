"""Customer-facing chatbot agent with governance-rule-driven system prompt."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models.chat import ChatMessage
from app.models.cs_governance import CSGovernanceRule
from app.models.equipment import EquipmentUnit
from app.models.settings import AppSetting
from app.services import google_calendar as gcal_svc

_log = logging.getLogger(__name__)

_MAX_TOOL_CALLS = 5
_MAX_HISTORY = 10
_MAX_TOKENS_CHAT = 380  # keeps responses concise; cuts generation time roughly in half vs 768
_RATE_LIMIT_PER_HOUR: dict[str, int] = {
    "anthropic": 20,
    "groq": 20,
    "openai": 20,
    "gemini": 20,
    "ollama": 500,
}
_RATE_LIMIT_DEFAULT = 20

_CATEGORY_LABELS = {
    "tone": "Tone & Style",
    "info_policy": "Information Policy",
    "escalation": "Escalation Rules",
    "knowledge": "Company Knowledge",
    "custom": "Additional Rules",
}

# Human-readable labels for equipment_type, mirrored from app/routers/equipment.py
# so the chatbot describes machines the same way the catalog does.
_EQUIPMENT_TYPE_LABELS = {
    "smart_cooler": "AI Smart Cooler",
    "freezer": "Smart Freezer",
    "combo": "Combo Machine",
    "drink": "Drink Machine",
    "snack": "Snack Machine",
    "glass_cooler": "Glass-Door Cooler",
    "kiosk": "Micro Market Kiosk",
    "micro_market": "Micro Market",
}

# Cap how many units we describe in the prompt so the context stays small and the
# public (Groq free-tier) model stays fast. The catalog is small; this is a guard.
_MAX_EQUIPMENT_FOR_PROMPT = 30

# ── Provider / model defaults ────────────────────────────────────────────────

# Public chatbot runs on Groq's free tier by default: $0 for client traffic,
# ~5-10x faster than Haiku, and Groq does not train on inputs/outputs. If Groq
# fails (rate limit / missing key / outage), get_chatbot_reply falls back to
# Claude Haiku so the widget never goes dark. Internal/admin AI is unaffected —
# those services call anthropic.Anthropic directly, not get_active_provider.
_DEFAULT_PROVIDER = "groq"
_DEFAULT_MODEL = "llama-3.1-8b-instant"
_FALLBACK_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

PROVIDER_MODELS: dict[str, list[str]] = {
    "anthropic": ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"],
    "groq": ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"],
    "openai": ["gpt-4o-mini", "gpt-4o"],
    "gemini": ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
    "ollama": ["llama3.2:3b", "phi4-mini", "mistral:7b"],
}


def get_active_provider(db: Session) -> tuple[str, str]:
    """Return (provider, model) from AppSetting, falling back to defaults."""
    p_row = db.get(AppSetting, "cs_ai_provider")
    m_row = db.get(AppSetting, "cs_ai_model")
    provider = (p_row.value if p_row else None) or _DEFAULT_PROVIDER
    model = (m_row.value if m_row else None) or _DEFAULT_MODEL
    return provider, model


# ── Equipment knowledge ──────────────────────────────────────────────────────


def _format_unit_line(unit: EquipmentUnit) -> str:
    """One compact bullet describing a unit's customer-relevant specs.

    Only includes fields that are populated so the chatbot never invents specs or
    parrots empty placeholders. Capacity is the field most asked about ("how many
    drinks fit?"), so it leads.
    """
    name_bits = [unit.manufacturer, unit.product_name]
    name = " ".join(b for b in name_bits if b).strip() or unit.product_name
    type_label = _EQUIPMENT_TYPE_LABELS.get(
        unit.equipment_type, unit.equipment_type.replace("_", " ").title()
    )

    specs: list[str] = []
    if unit.capacity_units:
        specs.append(f"holds ~{unit.capacity_units} items/products")
    if unit.capacity_cu_ft:
        specs.append(f"{unit.capacity_cu_ft:g} cu ft capacity")
    if unit.height_in and unit.width_in and unit.depth_in:
        specs.append(f'{unit.height_in:g}"H x {unit.width_in:g}"W x {unit.depth_in:g}"D')
    if unit.weight_lbs:
        specs.append(f"{unit.weight_lbs:g} lbs")
    if unit.payment_types:
        specs.append(f"payments: {unit.payment_types}")
    if unit.ai_features:
        acc = f" (~{unit.ai_accuracy_pct:g}% accuracy)" if unit.ai_accuracy_pct else ""
        specs.append(f"AI grab-and-go checkout{acc}")
    if unit.connectivity:
        specs.append(f"connectivity: {unit.connectivity}")

    # Deliberately no pricing. Equipment is installed at no cost to the host
    # business; the specific model is chosen during the on-site evaluation, so the
    # chatbot must never quote prices or imply the partner pays for a machine.

    detail = "; ".join(specs) if specs else "specs available on request"
    return f"- {name} ({type_label}): {detail}"


def build_equipment_knowledge(db: Session) -> str:
    """Render the active equipment catalog as a prompt section.

    The chatbot previously had no awareness of the catalog, so questions like
    "how many drinks can a machine hold?" went unanswered. This feeds the live
    catalog (active units only, archived excluded) into the system prompt.
    """
    units = (
        db.query(EquipmentUnit)
        .filter(EquipmentUnit.status == "active")
        .order_by(EquipmentUnit.equipment_type, EquipmentUnit.manufacturer)
        .limit(_MAX_EQUIPMENT_FOR_PROMPT)
        .all()
    )
    if not units:
        return ""

    lines = [_format_unit_line(u) for u in units]
    return (
        "EQUIPMENT CATALOG (use these real specs to answer questions about machine "
        "capacity, size, features, and connectivity — do not invent numbers; if a "
        "detail isn't listed, say you'll have a team member confirm):\n"
        + "\n".join(lines)
        + "\n\nEQUIPMENT COST & MODEL SELECTION (always follow this):\n"
        "  - The equipment we install comes at NO COST to the host business — the "
        "partner never buys or leases a machine.\n"
        "  - NEVER quote prices, dollar figures, or cost ranges for any unit, even if "
        "asked directly. We do not sell equipment to the partner.\n"
        "  - The specific equipment model is determined when our team comes out for an "
        "on-site evaluation of the location.\n"
        "  - When a customer asks about cost, pricing, or which machine they'd get, "
        "respond along these lines: the equipment is installed at no cost to the "
        "business, and the exact model is chosen during a free on-site evaluation. "
        'Then offer to schedule it, e.g. "Would you like to set up an appointment?"'
    )


# ── System prompt ────────────────────────────────────────────────────────────


def build_chatbot_system_prompt(db: Session, include_tools: bool = True) -> str:
    rules = (
        db.query(CSGovernanceRule)
        .filter(CSGovernanceRule.is_active.is_(True))
        .order_by(CSGovernanceRule.display_order, CSGovernanceRule.id)
        .all()
    )

    today = datetime.now(tz=gcal_svc.load_scheduling_config(db).tz)
    base = (
        f"You are a helpful customer service assistant for Prime Micro Markets, "
        f"a veteran-owned smart cooler vending company serving "
        f"Bay County, FL (Panama City area).\n\n"
        f"{settings.company_blurb}\n\n"
        f"You help answer questions from potential and existing host location partners.\n\n"
        f"Today's date is {today.strftime('%A, %B')} {today.day}, {today.year}.\n\n"
    )

    if rules:
        grouped: dict[str, list[CSGovernanceRule]] = {}
        for r in rules:
            grouped.setdefault(r.category, []).append(r)

        rule_sections = []
        for cat, cat_rules in grouped.items():
            label = _CATEGORY_LABELS.get(cat, cat.replace("_", " ").title())
            items = "\n".join(f"  - {r.rule_text}" for r in cat_rules)
            rule_sections.append(f"**{label}:**\n{items}")

        base += "RULES YOU MUST FOLLOW:\n" + "\n\n".join(rule_sections) + "\n\n"

    equipment_knowledge = build_equipment_knowledge(db)
    if equipment_knowledge:
        base += equipment_knowledge + "\n\n"

    if include_tools:
        scheduling_note = (
            "SCHEDULING A CONSULTATION — follow these steps in order:\n"
            "  1. FIRST ask the customer their preference: which day works best, and do they "
            "prefer morning or afternoon? Ask before listing any times.\n"
            "  2. Call check_availability with their stated 'day' and/or 'period' to fetch "
            "matching open slots, and show them as a short bulleted list. If they have no "
            "preference, call it with no filters. Help them narrow to one specific time.\n"
            "  3. Once they confirm a specific time, collect the details needed to book: their "
            "full name, phone number, and the location address for the proposed unit (email is "
            "optional but lets us send a calendar invite). Ask for anything still missing.\n"
            "  4. Read the chosen time and their details back to confirm, then call "
            "book_appointment with the slot's date and time exactly as you showed them, plus "
            "name, phone, and location. Only call book_appointment after they've confirmed.\n"
            "  Never invent times — only offer what check_availability returns. Never claim a "
            "booking is made unless book_appointment succeeded."
        )
    elif settings.google_booking_url:
        scheduling_note = (
            f"When asked about scheduling, direct them to book here: {settings.google_booking_url}"
        )
    else:
        scheduling_note = (
            "When asked about scheduling, ask them to email us at primemicromarkets@gmail.com."
        )

    if include_tools:
        escalation_note = (
            "When a question or situation is beyond your ability to resolve — such as legal matters, "
            "contract disputes, or situations requiring human judgement — use the "
            "request_human_followup tool."
        )
    else:
        escalation_note = (
            "When a question or situation is beyond your ability to resolve — such as legal matters, "
            "contract disputes, or situations requiring human judgement — ask the customer to email "
            "us at primemicromarkets@gmail.com and a team member will follow up."
        )

    lead_capture_note = (
        "CONTACT OPTIONS: When a customer asks about getting a unit, scheduling, pricing, "
        "or wants to be contacted, mention the ways to connect:\n"
        "  1. Book a consultation. If they ask about scheduling, FIRST call check_availability "
        "and show the returned times as a bulleted list, then point them to booking.\n"
        "  2. Email us at primemicromarkets@gmail.com\n"
        "  3. Click the 'Request a Callback' button below the chat input to leave their "
        "contact info so a real team member can personally reach out.\n"
        "When you have specific available times, lead with that list. Offer email and the "
        "callback button as alternatives, not as a substitute for showing the times."
    )

    base += (
        f"{scheduling_note}\n\n"
        f"{escalation_note}\n\n"
        f"{lead_capture_note}\n\n"
        f"Keep responses concise — 2 to 4 sentences maximum unless listing specific details. "
        f"Answer the question directly, then stop. You are representing a professional business."
    )
    return base


# ── Tool definitions ─────────────────────────────────────────────────────────

_TOOL_AVAILABILITY = {
    "name": "check_availability",
    "description": (
        "Check the company calendar and return open consultation time slots. Use this to "
        "narrow down a time AFTER asking the customer their preference. Pass the customer's "
        "stated preference as filters so the options match what they asked for."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "day": {
                "type": "string",
                "description": (
                    "Optional day-of-week preference, e.g. 'Monday' or 'Saturday'. "
                    "Omit if the customer has no day preference."
                ),
            },
            "period": {
                "type": "string",
                "enum": ["morning", "afternoon"],
                "description": "Optional time-of-day preference. Omit if no preference.",
            },
        },
        "required": [],
    },
}

_TOOL_BOOK = {
    "name": "book_appointment",
    "description": (
        "Book a CONFIRMED consultation on the company calendar. Call this ONLY after: "
        "(1) the customer picked a specific time you offered via check_availability, AND "
        "(2) they gave their full name, phone number, and the location address for the "
        "proposed unit. Pass the chosen slot's date and time exactly as you showed it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "The chosen slot's date as shown, e.g. 'June 6' or '2026-06-06'.",
            },
            "time": {
                "type": "string",
                "description": "The chosen slot's start time as shown, e.g. '10:00 AM'.",
            },
            "name": {"type": "string", "description": "Customer's full name"},
            "phone": {"type": "string", "description": "Customer's phone number"},
            "location": {
                "type": "string",
                "description": "Address of the location for the proposed unit",
            },
            "email": {
                "type": "string",
                "description": "Customer's email (optional — used to send a calendar invite)",
            },
            "notes": {"type": "string", "description": "Anything else relevant (optional)"},
        },
        "required": ["date", "time", "name", "phone", "location"],
    },
}

_TOOL_ESCALATE = {
    "name": "request_human_followup",
    "description": (
        "Use this when the customer needs a human team member — e.g., billing issues, "
        "legal matters, contract questions, or persistent frustration."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why human follow-up is needed",
            },
            "urgency": {
                "type": "string",
                "enum": ["normal", "high"],
                "description": "Urgency level",
            },
        },
        "required": ["reason"],
    },
}

# OpenAI-format equivalents (used for Groq, OpenAI, Gemini-OpenAI-compat)
_TOOL_AVAILABILITY_OAI = {
    "type": "function",
    "function": {
        "name": "check_availability",
        "description": _TOOL_AVAILABILITY["description"],
        "parameters": _TOOL_AVAILABILITY["input_schema"],
    },
}

_TOOL_ESCALATE_OAI = {
    "type": "function",
    "function": {
        "name": "request_human_followup",
        "description": _TOOL_ESCALATE["description"],
        "parameters": _TOOL_ESCALATE["input_schema"],
    },
}

_TOOL_BOOK_OAI = {
    "type": "function",
    "function": {
        "name": "book_appointment",
        "description": _TOOL_BOOK["description"],
        "parameters": _TOOL_BOOK["input_schema"],
    },
}

_TOOL_CAPTURE_LEAD = {
    "name": "capture_lead",
    "description": (
        "Save a site visitor's contact info to the sales pipeline so a real team member can "
        "follow up. Call this ONLY after the customer has reviewed a summary of their info "
        "and confirmed they want it submitted."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Customer's full name"},
            "email": {"type": "string", "description": "Customer's email address"},
            "phone": {"type": "string", "description": "Customer's phone number"},
            "location": {
                "type": "string",
                "description": "Address or description of the location for the proposed unit",
            },
            "description": {
                "type": "string",
                "description": "What the customer is looking for or their situation",
            },
        },
        "required": ["name"],
    },
}

_TOOL_CAPTURE_LEAD_OAI = {
    "type": "function",
    "function": {
        "name": "capture_lead",
        "description": _TOOL_CAPTURE_LEAD["description"],
        "parameters": _TOOL_CAPTURE_LEAD["input_schema"],
    },
}


def _handle_capture_lead(tool_input: dict, session_id: str, db: Session) -> str:
    from app.models.sales import OutreachLog, Prospect

    name = (tool_input.get("name") or "").strip() or "Site Visitor"
    email = (tool_input.get("email") or "").strip() or None
    phone = (tool_input.get("phone") or "").strip() or None
    location = (tool_input.get("location") or "").strip() or None
    description = (tool_input.get("description") or "").strip() or None

    company = (location or f"Inbound — {name}")[:200]
    notes_parts = []
    if description:
        notes_parts.append(f"Looking for: {description}")
    notes_parts.append(f"Chatbot session: {session_id[:8]}")

    prospect = Prospect(
        company_name=company,
        contact_name=name[:150],
        contact_email=email[:200] if email else None,
        contact_phone=phone[:30] if phone else None,
        address=location[:300] if location else None,
        source="AI Chatbot — Site Visitor",
        notes=" | ".join(notes_parts),
        pipeline_stage="lead",
    )
    db.add(prospect)
    db.flush()

    db.add(
        OutreachLog(
            prospect_id=prospect.id,
            channel="chatbot",
            direction="inbound",
            contacted_at=datetime.now(),
            subject_or_summary="Inbound inquiry via AI Chatbot widget",
            notes=(
                f"Site visitor submitted contact info through the public chatbot. "
                f"Chat session: {session_id[:8]}."
            ),
        )
    )
    db.commit()
    return (
        "Lead saved to the sales pipeline. A team member will be notified to personally follow up."
    )


_WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", re.IGNORECASE)


def _parse_weekday(text: str) -> int | None:
    text = (text or "").lower()
    for name, n in _WEEKDAY_NAMES.items():
        if name in text:
            return n
    return None


def _parse_period(text: str) -> str | None:
    text = (text or "").strip().lower()
    return text if text in ("morning", "afternoon") else None


def _parse_clock(text: str) -> tuple[int, int] | None:
    """'10:00 AM' / '2pm' / '14:00' -> (hour, minute) in 24h, or None."""
    m = _TIME_RE.search(text or "")
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").replace(".", "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _parse_month_day(text: str) -> tuple[int, int] | None:
    """'June 6' / '6 June' / '2026-06-06' / '6/6' -> (month, day), or None."""
    text = (text or "").strip().lower()
    iso = re.search(r"\b\d{4}-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        return int(iso.group(1)), int(iso.group(2))
    slash = re.search(r"\b(\d{1,2})/(\d{1,2})\b", text)
    if slash:
        return int(slash.group(1)), int(slash.group(2))
    for name, mon in _MONTH_NAMES.items():
        if name in text:
            day_m = re.search(r"\b(\d{1,2})\b", text)
            if day_m:
                return mon, int(day_m.group(1))
    return None


def _match_open_slot(db: Session, date_text: str, time_text: str):
    """Find the real open slot matching the customer's stated day + time.

    We never trust a model-built timestamp: we re-fetch the actual open slots and
    match on (hour, minute) plus the date or weekday the customer named. This is
    robust to small models that fumble ISO/timezone math, and guarantees we only
    book a slot that is genuinely open right now.
    """
    clock = _parse_clock(time_text)
    if clock is None:
        return None
    hour, minute = clock
    # Generous pool across the whole look-ahead window (ignore the display cap).
    slots = gcal_svc.get_open_slots(db, max_slots=10_000)
    candidates = [s for s in slots if s.hour == hour and s.minute == minute]
    if not candidates:
        return None

    month_day = _parse_month_day(date_text)
    weekday = _parse_weekday(date_text)
    for s in candidates:
        if month_day and (s.month, s.day) == month_day:
            return s
    for s in candidates:
        if weekday is not None and s.weekday() == weekday:
            return s
    # A date/day was named but nothing matched it — don't guess.
    if month_day or weekday is not None:
        return None
    return candidates[0]


def _handle_check_availability(tool_input: dict, db: Session) -> str:
    try:
        return gcal_svc.get_availability_message(
            db,
            weekday=_parse_weekday(tool_input.get("day", "")),
            period=_parse_period(tool_input.get("period", "")),
        )
    except Exception as exc:
        if settings.google_booking_url:
            return f"Book directly here: {settings.google_booking_url}"
        return (
            f"Unable to fetch availability ({exc}). "
            "Please email primemicromarkets@gmail.com to schedule."
        )


def _handle_book_appointment(tool_input: dict, session_id: str, db: Session) -> str:
    name = (tool_input.get("name") or "").strip()
    phone = (tool_input.get("phone") or "").strip()
    location = (tool_input.get("location") or "").strip()
    email = (tool_input.get("email") or "").strip() or None
    notes = (tool_input.get("notes") or "").strip() or None
    date_text = (tool_input.get("date") or "").strip()
    time_text = (tool_input.get("time") or "").strip()

    missing = [
        label
        for label, val in (
            ("name", name),
            ("phone", phone),
            ("location", location),
            ("a date", date_text),
            ("a time", time_text),
        )
        if not val
    ]
    if missing:
        return (
            "Before booking, ask the customer for the missing details: "
            + ", ".join(missing)
            + ". Do not book until you have a chosen time, name, phone, and location."
        )

    try:
        slot = _match_open_slot(db, date_text, time_text)
    except Exception as exc:
        _log.warning("Booking slot lookup failed: %s", exc)
        slot = None
    if slot is None:
        return (
            "That time isn't on the open list. Call check_availability again and offer the "
            "customer one of the exact times it returns, then book the one they choose."
        )

    label = gcal_svc._format_label(slot)
    summary = f"Prime Micro Markets consultation — {name}"
    desc_lines = [
        "Booked via the website chatbot.",
        f"Name: {name}",
        f"Phone: {phone}",
    ]
    if email:
        desc_lines.append(f"Email: {email}")
    desc_lines.append(f"Location: {location}")
    if notes:
        desc_lines.append(f"Notes: {notes}")
    desc_lines.append(f"Chat session: {session_id[:8]}")
    description = "\n".join(desc_lines)

    booked = False
    try:
        gcal_svc.create_event(
            db, start=slot, summary=summary, description=description, attendee_email=email
        )
        booked = True
    except gcal_svc.CalendarWriteError as exc:
        _log.warning(
            "Calendar booking unavailable (needs_reconnect=%s): %s",
            getattr(exc, "needs_reconnect", False),
            exc,
        )
    except Exception as exc:
        _log.warning("Calendar booking failed: %s", exc)

    try:
        _save_booking_lead(name, email, phone, location, notes, label, booked, session_id, db)
    except Exception:
        _log.exception("Failed to save booking lead")

    if booked:
        invite = " A calendar invite is on its way to your email." if email else ""
        return (
            f"{_BOOKING_CONFIRMED_MARKER} for {label}.{invite} "
            f"We have your details on file ({name}, {phone}) — we look forward to meeting you!"
        )
    return (
        f"Thanks {name}! I've recorded your requested time ({label}) along with your details, "
        "and a team member will confirm shortly by phone or email."
    )


def _save_booking_lead(
    name: str,
    email: str | None,
    phone: str | None,
    location: str | None,
    notes: str | None,
    slot_label: str,
    booked: bool,
    session_id: str,
    db: Session,
) -> None:
    from app.models.sales import OutreachLog, Prospect

    status = "Booked consultation" if booked else "Requested consultation (pending confirm)"
    note_parts = [f"{status}: {slot_label}"]
    if notes:
        note_parts.append(f"Notes: {notes}")
    note_parts.append(f"Chatbot session: {session_id[:8]}")

    prospect = Prospect(
        company_name=(location or f"Consultation — {name}")[:200],
        contact_name=name[:150],
        contact_email=email[:200] if email else None,
        contact_phone=phone[:30] if phone else None,
        address=location[:300] if location else None,
        source="AI Chatbot — Booked Consultation",
        notes=" | ".join(note_parts),
        pipeline_stage="qualified" if booked else "lead",
    )
    db.add(prospect)
    db.flush()
    db.add(
        OutreachLog(
            prospect_id=prospect.id,
            channel="chatbot",
            direction="inbound",
            contacted_at=datetime.now(),
            subject_or_summary=f"{status} via AI Chatbot",
            notes=f"{status} for {slot_label}. Session: {session_id[:8]}.",
        )
    )
    db.commit()


def _handle_tool(name: str, tool_input: dict, session_id: str, db: Session) -> str:
    if name == "check_availability":
        return _handle_check_availability(tool_input, db)

    if name == "book_appointment":
        return _handle_book_appointment(tool_input, session_id, db)

    if name == "capture_lead":
        return _handle_capture_lead(tool_input, session_id, db)

    if name == "request_human_followup":
        reason = tool_input.get("reason", "Not specified")
        urgency = tool_input.get("urgency", "normal")
        # Log escalation request
        db.add(
            ChatMessage(
                session_id=session_id,
                role="tool",
                tool_name="request_human_followup",
                content=json.dumps({"reason": reason, "urgency": urgency}),
            )
        )
        # Append to escalation pending list in AppSetting
        key = "chatbot_escalation_pending"
        row = db.get(AppSetting, key)
        try:
            existing = json.loads(row.value) if row else []
        except Exception:
            existing = []
        existing.append(
            {
                "session_id": session_id,
                "reason": reason,
                "urgency": urgency,
                "created_at": datetime.now().isoformat(),
            }
        )
        if row:
            row.value = json.dumps(existing[-50:])  # keep last 50
        else:
            db.add(AppSetting(key=key, value=json.dumps(existing[-50:])))
        db.commit()
        return "Human follow-up requested. A team member will reach out shortly."

    return "Unknown tool."


# ── Rate limiting ─────────────────────────────────────────────────────────────


def _is_rate_limited(session_id: str, db: Session, provider: str = "anthropic") -> bool:
    limit = _RATE_LIMIT_PER_HOUR.get(provider, _RATE_LIMIT_DEFAULT)
    cutoff = datetime.now() - timedelta(hours=1)
    count = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "user",
            ChatMessage.created_at >= cutoff,
        )
        .count()
    )
    return count >= limit


# ── History loader ────────────────────────────────────────────────────────────


def _load_history(session_id: str, db: Session) -> list[dict]:
    msgs = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == session_id,
            ChatMessage.role.in_(["user", "assistant"]),
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(_MAX_HISTORY)
        .all()
    )
    return [{"role": m.role, "content": m.content} for m in reversed(msgs)]


# ── Anthropic runner ─────────────────────────────────────────────────────────


def _run_anthropic(
    messages: list[dict],
    system: str,
    model: str,
    session_id: str,
    db: Session,
    captured: dict | None = None,
) -> str:
    import anthropic  # type: ignore[import-untyped]

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    tool_calls = 0

    while tool_calls <= _MAX_TOOL_CALLS:
        response = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS_CHAT,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[_TOOL_AVAILABILITY, _TOOL_BOOK, _TOOL_ESCALATE, _TOOL_CAPTURE_LEAD],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return "".join(b.text for b in response.content if hasattr(b, "text"))

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls += 1
                result = _handle_tool(block.name, block.input, session_id, db)
                if captured is not None and block.name == "check_availability":
                    captured["availability"] = result
                if captured is not None and block.name == "book_appointment":
                    captured["booking"] = result
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )
        if not tool_results:
            break
        messages.append({"role": "user", "content": tool_results})

    # Fallback: extract any text from the last assistant turn
    last = messages[-1] if messages else {}
    content = last.get("content", [])
    if isinstance(content, list):
        return (
            "".join(b.text for b in content if hasattr(b, "text"))
            or "I'm sorry, I couldn't generate a response."
        )
    return str(content) or "I'm sorry, I couldn't generate a response."


# ── OpenAI-compatible runner (Groq + OpenAI) ─────────────────────────────────


def _run_openai_compat(
    messages: list[dict],
    system: str,
    model: str,
    api_key: str,
    base_url: str | None,
    session_id: str,
    db: Session,
    max_tokens: int = _MAX_TOKENS_CHAT,
    use_tools: bool = True,
    captured: dict | None = None,
) -> str:
    try:
        from openai import OpenAI  # type: ignore[import-untyped]
    except ImportError:
        return "OpenAI SDK not installed. Run: pip install openai"

    client = OpenAI(api_key=api_key, **({"base_url": base_url} if base_url else {}))
    oai_messages = [{"role": "system", "content": system}] + [
        {
            "role": m["role"],
            "content": m["content"] if isinstance(m["content"], str) else str(m["content"]),
        }
        for m in messages
    ]

    if not use_tools:
        response = client.chat.completions.create(
            model=model,
            messages=oai_messages,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or "I'm sorry, I couldn't generate a response."

    tool_calls_count = 0

    while tool_calls_count <= _MAX_TOOL_CALLS:
        response = client.chat.completions.create(
            model=model,
            messages=oai_messages,
            tools=[
                _TOOL_AVAILABILITY_OAI,
                _TOOL_BOOK_OAI,
                _TOOL_ESCALATE_OAI,
                _TOOL_CAPTURE_LEAD_OAI,
            ],
            tool_choice="auto",
            max_tokens=max_tokens,
        )
        choice = response.choices[0]
        msg = choice.message
        oai_messages.append(
            {"role": "assistant", "content": msg.content or "", "tool_calls": msg.tool_calls}
        )

        if choice.finish_reason == "stop" or not msg.tool_calls:
            return msg.content or "I'm sorry, I couldn't generate a response."

        for tc in msg.tool_calls:
            tool_calls_count += 1
            try:
                tool_input = json.loads(tc.function.arguments)
            except Exception:
                tool_input = {}
            result = _handle_tool(tc.function.name, tool_input, session_id, db)
            if captured is not None and tc.function.name == "check_availability":
                captured["availability"] = result
            if captured is not None and tc.function.name == "book_appointment":
                captured["booking"] = result
            oai_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )

    return "I'm sorry, I encountered an issue generating a response."


# ── Gemini runner ─────────────────────────────────────────────────────────────


def _run_gemini(
    messages: list[dict],
    system: str,
    model: str,
    session_id: str,
    db: Session,
    captured: dict | None = None,
) -> str:
    # Use Gemini's OpenAI-compatible endpoint
    return _run_openai_compat(
        messages=messages,
        system=system,
        model=model,
        api_key=settings.gemini_api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        session_id=session_id,
        db=db,
        captured=captured,
    )


# ── Ollama lead extraction ────────────────────────────────────────────────────

_CONTACT_MARKER = "[CONTACT_NOTED]"
_CONTACT_MARKER_RE = re.compile(r"\*{0,2}\[CONTACT_NOTED\]\*{0,2}", re.IGNORECASE)


def _save_lead_from_conversation(session_id: str, db: Session) -> None:
    """Build a Prospect from the recent chatbot conversation history."""
    from app.models.sales import OutreachLog, Prospect

    msgs = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == session_id,
            ChatMessage.role.in_(["user", "assistant"]),
        )
        .order_by(ChatMessage.id)
        .limit(30)
        .all()
    )
    conversation = "\n".join(
        f"{'Customer' if m.role == 'user' else 'Assistant'}: {m.content}" for m in msgs
    )

    email_m = re.search(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b", conversation)
    phone_m = re.search(r"\b\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", conversation)
    email = email_m.group(0) if email_m else None
    phone = phone_m.group(0) if phone_m else None

    label = email if email else f"session {session_id[:8]}"
    prospect = Prospect(
        company_name=f"Chatbot Inquiry — {label}"[:200],
        contact_email=email[:200] if email else None,
        contact_phone=phone[:30] if phone else None,
        source="AI Chatbot — Site Visitor",
        notes=f"Conversation:\n{conversation}"[:2000],
        pipeline_stage="lead",
    )
    db.add(prospect)
    db.flush()
    db.add(
        OutreachLog(
            prospect_id=prospect.id,
            channel="chatbot",
            direction="inbound",
            contacted_at=datetime.now(),
            subject_or_summary="Inbound lead via AI Chatbot widget",
            notes=f"Session: {session_id[:8]}. Full conversation stored in notes.",
        )
    )
    db.commit()


def _extract_ollama_lead(reply: str, session_id: str, db: Session) -> str:
    """Detect [CONTACT_NOTED] marker, save lead from conversation, strip the marker."""
    if "CONTACT_NOTED" not in reply.upper():
        return reply
    try:
        _save_lead_from_conversation(session_id, db)
    except Exception:
        pass  # DB error must not break the chat response
    return _CONTACT_MARKER_RE.sub("", reply).strip()


# ── Main background task ─────────────────────────────────────────────────────


def _error_message(exc: Exception) -> str:
    return (
        f"I'm sorry, I ran into an issue. Please contact us at "
        f"primemicromarkets@gmail.com. (Error: {exc})"
    )


# Marker that google_calendar.format_slots_for_chat prefixes to a real slot list.
_AVAIL_SLOTS_MARKER = "Here are the next available consultation times"
# Customer-facing prefix on a successful booking confirmation.
_BOOKING_CONFIRMED_MARKER = "✅ Your consultation is confirmed"
# A clock time like "10:00 AM" — used to tell if the model already wrote out the
# slot times itself (timezone-agnostic, so it survives a configured-tz change).
_CLOCK_TIME_RE = re.compile(r"\d{1,2}:\d{2}\s*(?:AM|PM)", re.IGNORECASE)


def _ensure_availability_shown(reply: str, captured: dict) -> str:
    """Guarantee fetched openings appear in the reply.

    Smaller models often call check_availability, then summarize ("you can pick
    one of these times") without listing them. If the tool returned a real slot
    list and the model's reply doesn't already include the times, prepend the
    slot block so the customer always sees concrete options.
    """
    avail = captured.get("availability", "")
    if not avail.startswith(_AVAIL_SLOTS_MARKER):
        return reply  # tool unused, no openings, or a fallback link — leave as-is
    if _CLOCK_TIME_RE.search(reply):
        return reply  # model already listed the times; don't duplicate
    return f"{avail}\n\n{reply}".strip() if reply.strip() else avail


def _ensure_booking_shown(reply: str, captured: dict) -> str:
    """Guarantee a booking confirmation reaches the customer.

    If book_appointment ran, the tool result is already a clean customer-facing
    line. If the model's own reply doesn't restate the time, use/prepend the
    confirmation so the customer always gets a concrete outcome.
    """
    booking = captured.get("booking", "")
    if not booking:
        return reply
    if _CLOCK_TIME_RE.search(reply):
        return reply  # model already restated the booked time
    return f"{booking}\n\n{reply}".strip() if reply.strip() else booking


def _dispatch(
    provider: str,
    model: str,
    messages: list[dict],
    system: str,
    session_id: str,
    db: Session,
    captured: dict | None = None,
) -> str:
    """Route to the active provider. Raises on a missing key or unknown provider."""
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured.")
        return _run_anthropic(messages, system, model, session_id, db, captured=captured)

    if provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY not configured.")
        return _run_openai_compat(
            messages,
            system,
            model,
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            session_id=session_id,
            db=db,
            captured=captured,
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY not configured.")
        return _run_openai_compat(
            messages,
            system,
            model,
            api_key=settings.openai_api_key,
            base_url=None,
            session_id=session_id,
            db=db,
            captured=captured,
        )

    if provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY not configured.")
        return _run_gemini(messages, system, model, session_id, db, captured=captured)

    if provider == "ollama":
        reply = _run_openai_compat(
            messages,
            system,
            model,
            api_key="ollama",
            base_url=settings.ollama_base_url,
            session_id=session_id,
            db=db,
            max_tokens=1024,
            use_tools=False,
        )
        return _extract_ollama_lead(reply, session_id, db)

    raise RuntimeError(f"Unknown provider: {provider}")


def get_chatbot_reply(session_id: str, user_message: str, db: Session, before_id: int = 0) -> str:
    """Synchronous LLM call using the caller's DB session. Saves and returns the reply.

    The primary provider is Groq (free + fast). If it fails for any reason — rate
    limit, missing key during cutover, or an outage — and an Anthropic key is set,
    fall back to Claude Haiku so the public widget never goes dark. Only rare
    overflow ever reaches the paid API.
    """
    provider, model = get_active_provider(db)

    if _is_rate_limited(session_id, db, provider=provider):
        reply = (
            "You've sent a lot of messages recently. Please email us directly at "
            "primemicromarkets@gmail.com and we'll be happy to help!"
        )
        db.add(ChatMessage(session_id=session_id, role="assistant", content=reply))
        db.commit()
        return reply
    # Ollama (local small models) can't reliably handle tool schemas — disable them
    include_tools = provider != "ollama"
    system = build_chatbot_system_prompt(db, include_tools=include_tools)

    # Load history excluding the current user message to avoid duplicates
    q = db.query(ChatMessage).filter(
        ChatMessage.session_id == session_id,
        ChatMessage.role.in_(["user", "assistant"]),
    )
    if before_id:
        q = q.filter(ChatMessage.id < before_id)
    history_rows = q.order_by(ChatMessage.created_at.desc()).limit(_MAX_HISTORY).all()
    history = [{"role": m.role, "content": m.content} for m in reversed(history_rows)]
    messages = history + [{"role": "user", "content": user_message}]

    captured: dict = {}
    try:
        reply = _dispatch(provider, model, messages, system, session_id, db, captured)
    except Exception as primary_exc:
        # Reliability fallback: keep the widget answering on Claude Haiku.
        # This is logged at WARNING so production (Render) logs reveal when the
        # free Groq path is being bypassed for the paid fallback — i.e. whether
        # the zero-cost goal is actually holding. Frequent warnings here mean
        # Groq is failing (e.g. a 403 IP block) and traffic is silently costing money.
        if provider != "anthropic" and settings.anthropic_api_key:
            _log.warning(
                "Chatbot primary provider %r failed (%s); falling back to Claude Haiku.",
                provider,
                primary_exc,
            )
            try:
                reply = _run_anthropic(
                    messages, system, _FALLBACK_ANTHROPIC_MODEL, session_id, db, captured
                )
            except Exception as fb_exc:
                _log.error("Chatbot Anthropic fallback also failed: %s", fb_exc)
                reply = _error_message(primary_exc)
        else:
            _log.error(
                "Chatbot provider %r failed with no fallback available: %s", provider, primary_exc
            )
            reply = _error_message(primary_exc)

    reply = _ensure_availability_shown(reply, captured)
    reply = _ensure_booking_shown(reply, captured)
    db.add(ChatMessage(session_id=session_id, role="assistant", content=reply))
    db.commit()
    return reply
