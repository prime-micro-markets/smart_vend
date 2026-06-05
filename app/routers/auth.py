import base64
import json
import logging
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.views import templates

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — endpoint URL, not a secret

_ERROR_MESSAGES: dict[str, str] = {
    "oauth_failed": "Google sign-in failed. Please try again.",
    "no_userinfo": "Could not retrieve your Google account info. Please try again.",
    "google_unavailable": (
        "Could not reach Google's sign-in service — this is usually a brief network hiccup. "
        "Wait a few seconds and try again."
    ),
    "state_mismatch": "Login session expired or was tampered with. Please try again.",
}


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/dashboard", error: str = "") -> HTMLResponse:
    if request.session.get("user"):
        return RedirectResponse(next, status_code=302)
    request.session["next_url"] = next
    friendly_error = _ERROR_MESSAGES.get(error, error)
    return templates.TemplateResponse(request, "auth/login.html", {"error": friendly_error})


@router.get("/auth/google")
async def google_login(request: Request) -> RedirectResponse:
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state

    redirect_uri = str(request.url_for("google_callback"))
    params = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
    )
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{params}", status_code=302)


@router.get("/auth/callback", name="google_callback")
async def google_callback(request: Request) -> RedirectResponse:
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    expected_state = request.session.pop("oauth_state", None)

    if not code or not state or state != expected_state:
        return RedirectResponse("/login?error=state_mismatch", status_code=302)

    redirect_uri = str(request.url_for("google_callback"))

    # Exchange the authorization code for tokens — single network call, 30-second timeout.
    # We avoid authlib's automatic discovery-URL and JWKS fetches, which were timing out
    # through NordVPN and causing Cloudflare to cancel the connection.
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            resp.raise_for_status()
            token_data = resp.json()
    except Exception:
        logger.exception("Google OAuth token exchange failed")
        return RedirectResponse("/login?error=oauth_failed", status_code=302)

    # Decode the ID token claims without JWKS signature verification.
    # The code came directly from Google's OAuth server, so we trust the payload.
    id_token_str = token_data.get("id_token", "")
    if not id_token_str:
        return RedirectResponse("/login?error=no_userinfo", status_code=302)

    try:
        payload_b64 = id_token_str.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims: dict = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        logger.exception("Failed to decode Google ID token payload")
        return RedirectResponse("/login?error=no_userinfo", status_code=302)

    email = claims.get("email", "")
    if not email:
        return RedirectResponse("/login?error=no_userinfo", status_code=302)

    if settings.allowed_emails:
        allowed = {e.strip().lower() for e in settings.allowed_emails.split(",")}
        if email.lower() not in allowed:
            return templates.TemplateResponse(
                request,
                "auth/login.html",
                {"error": f"Access denied for {email}. Contact your administrator to be added."},
                status_code=403,
            )

    request.session["user"] = {
        "email": email,
        "name": claims.get("name", email),
        "picture": claims.get("picture", ""),
    }

    next_url = request.session.pop("next_url", "/dashboard")
    return RedirectResponse(next_url, status_code=302)


@router.get("/auth/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/", status_code=302)
