# Verify outbound TLS against the OS trust store before any HTTPS happens.
# Needed on dev machines behind an HTTPS-intercepting proxy/AV whose root CA
# OpenSSL/certifi rejects; harmless in production (Render/Linux). Must run first.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import asyncio  # noqa: I001 — keep truststore injection above all imports
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session as DBSession
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import settings
from app.database import Base, engine
from app.models import crm as _crm_models  # noqa: F401 — registers CRM models with Base
from app.models import sam_contract as _sam_contract_models  # noqa: F401 — registers SavedContract
from app.models import settings as _settings_models  # noqa: F401 — registers AppSetting with Base
from app.routers import crm as crm_router
from app.routers import financial, inventory, leads, locations, research, root, sales
from app.routers import settings as settings_router
from app.routers import auth as auth_router
from app.routers import chatbot as chatbot_router
from app.routers import customer_service as cs_router
from app.routers import equipment as equipment_router
from app.routers import public as public_router
from app.services.auth import require_user

_log = logging.getLogger(__name__)

# In-process inbox poller cadence.
_POLL_STARTUP_DELAY_SECONDS = 30  # let startup/health check settle before first poll
_DEFAULT_POLL_INTERVAL_MINUTES = 10

_LEAD_CAPTURE_RULE_TITLE = "Lead Capture — Callback Option"
_LEAD_CAPTURE_RULE_TEXT = (
    "LEAD CAPTURE: When a potential customer expresses interest in getting a unit installed "
    "or wants to be contacted, proactively offer three options: "
    "(1) schedule a consultation (use the check_availability tool to show "
    "open times on our calendar), "
    "(2) email us directly at primemicromarkets@gmail.com, or "
    "(3) leave their contact info here for a personal callback from a real team member. "
    "If they choose option 3, collect their full name, email address, phone number, "
    "the location or address where they want the unit installed, and a brief description "
    "of their needs — ONE question at a time, only asking for information not already "
    "shared in the conversation. Once all details are collected, present a clear summary "
    "and ask the customer to confirm before submitting. Make clear that a real person — "
    "not an AI — will personally reach out to them."
)


def _seed_governance_rules(db: DBSession) -> None:
    from app.models.cs_governance import CSGovernanceRule

    exists = db.query(CSGovernanceRule).filter_by(title=_LEAD_CAPTURE_RULE_TITLE).first()
    if not exists:
        db.add(
            CSGovernanceRule(
                category="escalation",
                title=_LEAD_CAPTURE_RULE_TITLE,
                rule_text=_LEAD_CAPTURE_RULE_TEXT,
                is_active=True,
                display_order=50,
            )
        )
        db.commit()
    elif "Calendly" in exists.rule_text:
        # One-time migration of the stale seeded default off Calendly. Scoped to rows
        # that still mention Calendly so a rule an admin rewrote by hand in the
        # Governance UI is left untouched.
        exists.rule_text = _LEAD_CAPTURE_RULE_TEXT
        db.commit()


def _autopoll_config() -> tuple[bool, int, bool]:
    """Read (enabled, interval_minutes, gmail_connected) from AppSetting.

    Runs in a worker thread (sync DB), so it owns a short-lived session.
    """
    from app.models.settings import AppSetting

    with DBSession(engine) as db:
        enabled_row = db.get(AppSetting, "gmail_autopoll_enabled")
        interval_row = db.get(AppSetting, "gmail_poll_interval_minutes")
        gmail_row = db.get(AppSetting, "gmail_refresh_token")

    enabled = (enabled_row.value if enabled_row else "1") != "0"
    try:
        interval = int(interval_row.value) if interval_row and interval_row.value else 0
    except ValueError:
        interval = 0
    interval = interval or _DEFAULT_POLL_INTERVAL_MINUTES
    gmail_connected = bool(gmail_row and gmail_row.value)
    return enabled, max(1, interval), gmail_connected


async def _inbox_poll_loop() -> None:
    """Poll the connected Gmail inbox on a configurable interval, forever.

    The poller is sync (httpx + a sync Session), so each step runs off the event
    loop via ``asyncio.to_thread``. One failing iteration never kills the loop.
    """
    await asyncio.sleep(_POLL_STARTUP_DELAY_SECONDS)
    while True:
        interval = _DEFAULT_POLL_INTERVAL_MINUTES
        try:
            enabled, interval, connected = await asyncio.to_thread(_autopoll_config)
            if enabled and connected:
                from app.services import gmail_monitor

                created = await asyncio.to_thread(gmail_monitor.poll_and_process)
                if created:
                    _log.info("Inbox poll filed %d new email(s).", len(created))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep the loop alive across failures
            # GmailReauthRequiredError is the expected, recoverable failure: the
            # token row has already been cleared and the UI now shows a
            # reconnect banner, so log it once at INFO and move on. Anything
            # else stays a WARNING so genuine bugs aren't swallowed.
            from app.services.gmail_monitor import GmailReauthRequiredError

            if isinstance(exc, GmailReauthRequiredError):
                _log.info(
                    "Gmail OAuth rejected; cleared local token. "
                    "Reconnect at /customer-service/gmail/connect. (%s)",
                    exc,
                )
            else:
                _log.warning("Inbox poll iteration failed: %s", exc)
        await asyncio.sleep(interval * 60)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    Base.metadata.create_all(bind=engine)
    with DBSession(engine) as db:
        _seed_governance_rules(db)
    poll_task = asyncio.create_task(_inbox_poll_loop())
    try:
        yield
    finally:
        poll_task.cancel()
        with suppress(asyncio.CancelledError):
            await poll_task


app = FastAPI(title=settings.app_title, lifespan=lifespan)

_static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Public routes — no auth required
app.include_router(public_router.router)
app.include_router(auth_router.router)
app.include_router(chatbot_router.router)

# Protected internal routes — require Google sign-in
_auth = [Depends(require_user)]
app.include_router(root.router, dependencies=_auth)
app.include_router(equipment_router.router, dependencies=_auth)
app.include_router(research.router, dependencies=_auth)
app.include_router(financial.router, dependencies=_auth)
app.include_router(locations.router, dependencies=_auth)
app.include_router(sales.router, dependencies=_auth)
app.include_router(inventory.router, dependencies=_auth)
app.include_router(leads.router, dependencies=_auth)
app.include_router(cs_router.router, dependencies=_auth)
app.include_router(crm_router.router, dependencies=_auth)
app.include_router(settings_router.router, dependencies=_auth)

# SessionMiddleware must be added last so it wraps everything (outermost = first to run)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, https_only=True)
# Trust X-Forwarded-Proto/For headers from Cloudflare Tunnel
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")


@app.middleware("http")
async def redirect_www_to_apex(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """301-redirect the www subdomain to the bare apex so one canonical host is indexed."""
    host = request.headers.get("host", "")
    if host.startswith("www."):
        target = request.url.replace(scheme="https", netloc=host[4:])
        return RedirectResponse(str(target), status_code=301)
    return await call_next(request)
