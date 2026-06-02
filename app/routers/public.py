import logging
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from app.views import templates

logger = logging.getLogger(__name__)
router = APIRouter(tags=["public"])

# Canonical public host. The www->apex redirect (see app/main.py) guarantees
# this is the single host crawlers ever resolve to, so every SEO URL uses it.
SITE_URL = "https://primemicromarkets.com"


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "public/landing.html", {})


_ROBOTS_TXT = f"""\
User-agent: *
Allow: /$
Allow: /chatbot/
Disallow: /dashboard
Disallow: /equipment/
Disallow: /research/
Disallow: /financial/
Disallow: /locations/
Disallow: /sales/
Disallow: /inventory/
Disallow: /leads/
Disallow: /customer-service/
Disallow: /crm/
Disallow: /settings/
Disallow: /auth/
Sitemap: {SITE_URL}/sitemap.xml
"""


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt() -> PlainTextResponse:
    # Must be served from the site root: StaticFiles is mounted at /static, so a
    # file there would not answer /robots.txt. This only guides crawling; the
    # noindex meta in base.html is the hard guarantee for the protected app.
    return PlainTextResponse(_ROBOTS_TXT)


@router.get("/sitemap.xml")
async def sitemap_xml() -> Response:
    # Only the public marketing surface belongs here. The whole internal app is
    # Disallow'd in robots.txt and carries noindex, so it stays out of the index.
    lastmod = date.today().isoformat()
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{SITE_URL}/</loc>"
        f"<lastmod>{lastmod}</lastmod>"
        "<changefreq>weekly</changefreq><priority>1.0</priority></url>\n"
        "</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")


@router.post("/contact", response_class=HTMLResponse)
async def contact(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    company: str = Form(...),
    venue_type: str = Form(...),
    daily_traffic: str = Form(""),
    message: str = Form(""),
) -> HTMLResponse:
    from app.config import settings
    from app.services.email_sender import send_email

    sent = False
    try:
        if settings.gmail_user and settings.gmail_app_password:
            body = (
                f"New location assessment request from {first_name} {last_name}\n\n"
                f"Email: {email}\n"
                f"Phone: {phone}\n"
                f"Company: {company}\n"
                f"Venue Type: {venue_type}\n"
                f"Daily Traffic: {daily_traffic}\n\n"
                f"Message:\n{message}"
            )
            send_email(
                to_address=settings.gmail_user,
                subject=f"New Assessment Request – {company}",
                body=body,
            )
            sent = True
        else:
            # No mail transport configured — log the lead so it isn't lost.
            logger.warning(
                "Contact form received but email not configured: %s %s <%s> / %s",
                first_name, last_name, email, company,
            )
            sent = True
    except Exception:
        logger.exception("Contact form email send failed for %s", company)
        sent = False

    ctx = {"sent": sent, "first_name": first_name}

    # HTMX submit → return just the result banner fragment. Always 200 so
    # htmx performs the swap; the fragment itself conveys success/error.
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request, "public/_contact_result.html", ctx
        )

    # Non-JS fallback → full page reload with success/error block
    return templates.TemplateResponse(
        request,
        "public/landing.html",
        {"contact_success": sent, "contact_error": not sent},
    )
