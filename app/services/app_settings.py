"""Runtime-configurable settings backed by the AppSetting table.

Lets the Claude model and agent tool/batch limits be changed from the
Settings UI without a code deploy. Bad/missing values fall back to the
documented defaults so a job never crashes on a typo.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Defaults — used when the AppSetting row is absent or unparseable.
DEFAULTS: dict[str, str] = {
    "research_model": "claude-haiku-4-5-20251001",
    "email_model": "claude-haiku-4-5-20251001",
    "inventory_model": "claude-haiku-4-5-20251001",
    "equipment_model": "claude-haiku-4-5-20251001",
    "research_max_tool_calls": "15",
    "inventory_max_tool_calls": "10",
    "equipment_batch_size": "4",
    # Chatbot scheduling / calendar window (consumed by app/services/google_calendar.py).
    # cal_day_hours is a JSON object mapping weekday (0=Mon … 6=Sun) -> [open_hour, close_hour]
    # on a 24-hour clock; a day absent from the map is closed (no times offered).
    "cal_day_hours": '{"0": [9, 17], "1": [9, 17], "2": [9, 17], "3": [9, 17], "4": [9, 17]}',
    "cal_slot_minutes": "30",
    "cal_lookahead_days": "7",
    "cal_max_slots": "8",
    "cal_timezone": "America/Chicago",
}


def get_str(db: Session, key: str) -> str:
    """Return the configured string value for ``key`` (or its default)."""
    from app.models.settings import AppSetting

    default = DEFAULTS.get(key, "")
    row = db.get(AppSetting, key)
    value = (row.value if row else "").strip()
    return value or default


def get_int(db: Session, key: str, *, minimum: int = 1, maximum: int = 100) -> int:
    """Return the configured int for ``key``, clamped to [minimum, maximum]."""
    default = int(DEFAULTS.get(key, "1"))
    from app.models.settings import AppSetting

    row = db.get(AppSetting, key)
    raw = (row.value if row else "").strip()
    if not raw:
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except ValueError:
        logger.warning("Invalid int for AppSetting %r=%r; using default %d", key, raw, default)
        return default
