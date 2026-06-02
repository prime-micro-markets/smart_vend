"""Settings dashboard — AI model/limits, search provider, and vendor config.

All values persist in the AppSetting key-value table so they can change at
runtime without a code deploy. API keys (Anthropic/Tavily/Firecrawl) come
from .env and are shown read-only as a configured/not-configured status.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings as env_settings
from app.database import get_db
from app.services import app_settings
from app.services.price_comparator import (
    VENDOR_KEYS,
    _setting_keys,
    load_vendor_settings,
)
from app.views import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

# AI/agent tunables surfaced in the dashboard. (key, label, kind, help)
_AI_FIELDS: list[tuple[str, str, str, str]] = [
    ("research_model", "Lead Research Model", "str", "Claude model for lead-gen research"),
    ("email_model", "Email Draft Model", "str", "Claude model for outreach drafting"),
    ("inventory_model", "Inventory Sourcing Model", "str", "Claude model for supplier sourcing"),
    ("equipment_model", "Equipment Refresh Model", "str", "Claude model for spec extraction"),
    ("research_max_tool_calls", "Research Max Tool Calls", "int", "Search budget per job (1–50)"),
    ("inventory_max_tool_calls", "Inventory Max Tool Calls", "int", "Search budget per job (1–30)"),
    ("equipment_batch_size", "Equipment Batch Size", "int", "Units per extraction batch (1–10)"),
]

_SEARCH_PROVIDERS = ["duckduckgo", "tavily"]


def _set_setting(db: Session, key: str, value: str) -> None:
    from app.models.settings import AppSetting

    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def _vendor_field_layout() -> dict[str, dict[str, str]]:
    """Per-vendor {field: setting_key} map, for rendering the vendor form."""
    return {vk: _setting_keys(vk) for vk in VENDOR_KEYS}


@router.get("/", response_class=HTMLResponse)
def settings_index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    ai_values = {key: app_settings.get_str(db, key) for key, _, _, _ in _AI_FIELDS}
    api_status = {
        "Anthropic (Claude)": bool(env_settings.anthropic_api_key),
        "Tavily": bool(env_settings.tavily_api_key),
        "Firecrawl": bool(env_settings.firecrawl_api_key),
    }
    return templates.TemplateResponse(
        request,
        "settings/index.html",
        {
            "active_nav": "settings",
            "ai_fields": _AI_FIELDS,
            "ai_values": ai_values,
            "ai_defaults": app_settings.DEFAULTS,
            "search_provider": app_settings.get_str(db, "search_provider") or "duckduckgo",
            "search_providers": _SEARCH_PROVIDERS,
            "vendor_layout": _vendor_field_layout(),
            "vendor_values": load_vendor_settings(db),
            "api_status": api_status,
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.post("/")
async def settings_save(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()

    # AI / agent tunables
    for key, _, kind, _ in _AI_FIELDS:
        raw = str(form.get(key, "")).strip()
        if not raw:
            continue
        if kind == "int":
            try:
                raw = str(int(raw))
            except ValueError:
                logger.warning("Ignored non-int settings value %r=%r", key, raw)
                continue
        _set_setting(db, key, raw)

    # Search provider
    provider = str(form.get("search_provider", "")).strip()
    if provider in _SEARCH_PROVIDERS:
        _set_setting(db, "search_provider", provider)

    # Vendor config — every setting_key across all vendors
    for keys in _vendor_field_layout().values():
        for setting_key in keys.values():
            if setting_key in form:
                _set_setting(db, setting_key, str(form.get(setting_key, "")).strip())

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to save settings")
        return RedirectResponse(url="/settings/?saved=0", status_code=303)

    return RedirectResponse(url="/settings/?saved=1", status_code=303)
