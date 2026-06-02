# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`smart_vend` is the internal management platform for Prime Micro Markets, a veteran-owned smart cooler vending business (51% Stephen Russell Troup, veteran; 49% John Michael Johnson) based in Panama City, FL. The company is pursuing VOB (Veteran-Owned Business) certification. Public domain: `primemicromarkets.com`.

## Commands

```bash
# Run dev server
uvicorn app.main:app --reload

# Lint / format
ruff check .
ruff check . --fix
ruff format .

# Tests
pytest
pytest path/to/test_file.py::test_name   # single test

# Database migrations
alembic revision --autogenerate -m "describe change"
alembic upgrade head
alembic current

# Seed research tasks (idempotent)
python scripts/seed_research_tasks.py "path/to/Research_Tracker.md"
```

## Tech Stack

FastAPI + Uvicorn · SQLite (dev) / Postgres (prod, psycopg 3) + SQLAlchemy 2.0 (sync) + Alembic · Jinja2 + Bootstrap 5.3 · HTMX 1.9 · pydantic-settings · Anthropic Claude + Groq + Tavily · Hosted on Render · Ruff · pytest

## Architecture

### App factory (`app/main.py`)

`main.py` imports all routers and mounts them. The lifespan handler calls `Base.metadata.create_all()` on startup (auto-creates tables, no migration needed for fresh installs). `SessionMiddleware` must be added last (outermost). `ProxyHeadersMiddleware` is required to trust `X-Forwarded-Proto` from the hosting proxy (Render in prod, formerly Cloudflare Tunnel); without it, OAuth redirects break. A `www`→apex 301 redirect middleware is registered outermost so one canonical host is served.

`app/models/settings.py` must be imported in `main.py` via a side-effect import (`from app.models import settings as _settings_models`) to register `AppSetting` with `Base` before `create_all` runs.

### Auth (`app/routers/auth.py`, `app/services/auth.py`)

Google OAuth via `authlib` + Starlette `SessionMiddleware`. `require_user` is a FastAPI dependency injected via `dependencies=[Depends(require_user)]` on protected routers in `main.py`. Public routes (`/`, `/login`, `/auth/*`, `/chatbot/*`) are mounted without this dependency.

### HTMX partial rendering pattern

Routers detect `request.headers.get("HX-Request") == "true"` and return partial templates (prefixed `_`) instead of full pages. Example: `GET /equipment/` returns `equipment/index.html` normally but `equipment/_unit_grid.html` for HTMX filter swaps.

### Background jobs (AgentJob pattern)

Long-running AI tasks (equipment refresh, lead research, email drafting) use FastAPI `BackgroundTasks`. A job row is written to `agent_jobs` with `status="pending"`, then the background function updates it through `running` → `done`/`error`. HTMX polls `/equipment/refresh/{job_id}/poll` (or equivalent) to stream status back to the UI. `AgentJob.agent_log` stores a JSON list of event dicts. `AgentJob.prospects_created` is overloaded for the equipment refresh job to store `units_updated`.

### AppSetting

Key-value config persisted in SQLite (`app_settings` table). Currently used to remember `search_provider` (duckduckgo vs tavily) between equipment refresh runs, and `equipment_curated_v1` (a sentinel so `scripts/curate_equipment.py` applies only once). Accessed via `_get_setting` / `_set_setting` helpers in routers.

### Equipment sourcing & curation

The equipment catalog (`/equipment/`) is a procurement tool: each `EquipmentUnit` has many `EquipmentSource` rows (one per `Distributor`), so the team compares prices across suppliers (A&M Equipment Sales, VendGuys, Cantaloupe, etc.). `EquipmentUnit.recompute_best_price()` denormalizes the lowest source price onto `price_low/price_high` for fast catalog rendering; `best_source` drives the "best buy" badge. Units are soft-archived (`status="archived"`), never deleted. `is_locked=True` marks curated/verified rows the AI spec-refresh job must skip (it filters out `is_locked`/archived units) — this stopped the price drift that came from unguarded auto-refreshes. `price_is_starting` renders a "Starting at" label for custom-quoted micro-market packages/kiosks. Catalog data is established by three scripts run in `render.yaml` preDeploy: `seed_distributors.py` (idempotent), `curate_equipment.py` (sentinel-guarded; fixes/archives/sources units + adds the curated lineup), and `fetch_equipment_images.py` (downloads real product photos to slug-named files under `static/images/equipment/`, committed so they survive Render's ephemeral FS).

### Templates

`app/views.py` creates the single shared `Jinja2Templates` instance and registers a `fromjson` filter. All routers import `templates` from there. Templates live in `app/templates/<module>/`.

## Site Layout

| URL prefix | Module | Auth |
|---|---|---|
| `/` | Public landing page | None |
| `/login`, `/auth/*` | Google OAuth flow | None |
| `/chatbot/*` | Customer-facing chatbot | None |
| `/dashboard` | Summary dashboard | Required |
| `/equipment/` | Equipment Catalog + AI spec refresh | Required |
| `/research/` | Research task board | Required |
| `/financial/` | Pro-forma P&L calculator | Required |
| `/locations/` | Locations & machine assignment | Required |
| `/sales/` | Sales pipeline (Kanban) | Required |
| `/inventory/` | Product catalog + restock log | Required |
| `/leads/` | AI lead generation + email outreach | Required |
| `/customer-service/` | CS governance/approval queue | Required |

## Key Config (`.env`)

```ini
DATABASE_URL=sqlite:///./smart_vend.db
ANTHROPIC_API_KEY=sk-ant-...
TAVILY_API_KEY=tvly-...
GMAIL_USER=primemicromarkets@gmail.com
GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
SESSION_SECRET_KEY=...
ALLOWED_EMAILS=comma,separated,list
GROQ_API_KEY=gsk_...
```

The app starts without any API keys set; AI features return errors until configured. Unknown env vars are ignored (`extra="ignore"` in `app/config.py`), so retired keys won't crash startup. `GROQ_API_KEY` powers the public chatbot (free tier) with a Claude Haiku fallback.

## Database Notes

- Production runs on **Render managed Postgres**; local dev uses SQLite (`smart_vend.db`, gitignored). `app/database.py` branches engine config on the URL scheme and normalizes Postgres URLs to the psycopg 3 driver.
- Tests use in-memory SQLite; no credentials needed.
- Schema changes require an Alembic migration; `create_all` only adds missing tables, does not alter columns. On deploy, `scripts/init_db.py` runs `create_all` + stamp on an empty DB, else `alembic upgrade head` (the initial migration is an empty baseline, so a bare `alembic upgrade head` cannot build from scratch).

## Ops (Render)

Production is hosted on **Render**, deployed from `main` via the `render.yaml` blueprint. Live site: `https://primemicromarkets.com` (apex; `www` 301-redirects to it). DNS + registrar: Cloudflare.

- **Resources:** web service `srv-d89ldt6l51nc73d8v6rg` (smart-vend), managed Postgres `dpg-d89lddml51nc73d8v3t0-a` (smart-vend-db), region `virginia`.
- **Deploys:** push to `main` → auto-deploy. Pipeline: build `pip install -r requirements.txt` → preDeploy `python scripts/init_db.py` → start `uvicorn`. A failed build/preDeploy/health-check keeps the previous version live (zero-downtime).
- **Secrets:** set as Render env vars in the dashboard, never committed. `DATABASE_URL` is injected from the managed Postgres; `SESSION_SECRET_KEY` is auto-generated.
- **Claude-managed:** the Render **MCP server** is configured (hosted `https://mcp.render.com/mcp`; API key in local `~/.claude.json`, not committed). After a Claude Code restart loads it, Claude can list services, trigger deploys, tail logs, manage env vars, and query Postgres directly.
- **SEO (live):** `public.py` serves `/robots.txt` (public surfaces allowed, internal app disallowed) and `/sitemap.xml` (homepage, dynamic `lastmod`). `landing.html` carries canonical, favicons, Open Graph + Twitter cards, `index,follow`, and a `LocalBusiness` JSON-LD (veteran-owned, Panama City FL — geo, areaServed, contactPoint, offer). `base.html` puts `noindex,nofollow` on the whole internal app. Covered by `tests/test_seo.py`. (The original `feature/seo-enhancements` branch is obsolete; this was re-applied onto current `main`.)
- **One-time data migration:** `scripts/migrate_sqlite_to_postgres.py` (used for the initial SQLite→Postgres cutover).
