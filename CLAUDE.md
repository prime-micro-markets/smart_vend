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

Google OAuth via `authlib` + Starlette `SessionMiddleware`. `require_user` is a FastAPI dependency injected via `dependencies=[Depends(require_user)]` on protected routers in `main.py`. Public routes (`/`, `/login`, `/auth/*`, `/chatbot/*`) are mounted without this dependency; everything else (including the Inventory cost-import and Sam's paste pages) requires sign-in.

### HTMX partial rendering pattern

Routers detect `request.headers.get("HX-Request") == "true"` and return partial templates (prefixed `_`) instead of full pages. Example: `GET /equipment/` returns `equipment/index.html` normally but `equipment/_unit_grid.html` for HTMX filter swaps.

### Background jobs (AgentJob pattern)

Long-running AI tasks (equipment refresh, lead research, email drafting) use FastAPI `BackgroundTasks`. A job row is written to `agent_jobs` with `status="pending"`, then the background function updates it through `running` → `done`/`error`. HTMX polls `/equipment/refresh/{job_id}/poll` (or equivalent) to stream status back to the UI. `AgentJob.agent_log` stores a JSON list of event dicts. `AgentJob.prospects_created` is overloaded for the equipment refresh job to store `units_updated`.

### AppSetting

Key-value config persisted in SQLite (`app_settings` table). Currently used to remember `search_provider` (duckduckgo vs tavily) between equipment refresh runs and `equipment_curated_v1` (a sentinel so `scripts/curate_equipment.py` applies only once). Accessed via `_get_setting` / `_set_setting` helpers in routers.

### Cost entry & Sam's Club purchase-history import

Sam's Club product/pricing endpoints sit behind Akamai bot protection, so a server-side fetch from Render's datacenter IP is permanently blocked. Sam's is therefore **not** dispatched by the price comparator (`COMPARATOR_FETCH_KEYS` excludes it; `VENDOR_KEYS` still lists it so its Club ID / ZIP stay editable for reference). Rather than scrape, the team enters real costs through three reliable, dependency-free paths in the Inventory tab — all of which upsert a `Sam's Club` `ProductSource` via `_upsert_product_source_from_comparison` and feed the comparator/margins:

- **Bulk cost grid** (`_bulk_costs.html` → `POST /inventory/costs/bulk`): a case-price + pack input per SKU, one Save-all writes a `ProductSource(origin="bulk_entry")` per filled row. The route lives under `/costs/` so it doesn't collide with the `/{product_id}` catch-all.
- **CSV import** (`GET/POST /inventory/costs/import` + `/costs/import/template`): match each row by SKU then exact name, upsert `origin="bulk_entry"`, report skipped/unmatched rows back.
- **Sam's purchase-history paste** (`GET /inventory/costs/sams-paste`, `POST .../preview`, `POST .../save`): a **no-token** bookmarklet (`static/js/sams_orders_capture.js`) reads the operator's own logged-in `samsclub.com/orders` page (embedded Next.js/redux state, DOM fallback) and copies line items to the clipboard. The operator pastes into an authenticated app page (behind Google login); `app/services/sams_paste.py::parse_sams_paste` tolerantly parses JSON / CSV export / copied tables, the preview renders a review/match table (best-guess SKU pre-selected), and Save writes `ProductSource(origin="sams_purchase")` with the *actual paid price* (instant savings included).

`parse_products_payload()` / `search_products()` in `sams_club.py` remain for **local testing only** (host IP isn't blocked off-Render); they are not wired into any live path. **Walmart was removed entirely** (unreliable scrape, prices not worth it). The earlier cross-origin **token-gated `/ingest` router, `ingest_token.py`, the `sams_capture.js` bookmarklet, and the `sams_bulk_lookup.py` Playwright helper were all retired** — the token couldn't survive the local↔prod DB split and Playwright's Chrome was Akamai-detected; the paste flow needs neither.

### Market reference (read-only competitor pricing)

The comparator has two sides. The **cost side** is `ProductSource` rows (above) — the only thing that touches `effective_cost`/`margin_pct`. The **market side** (`app/services/market_reference.py`) is *reference only*: what other sellers charge for a similar item, so the operator can sanity-check **sell** prices. It is never written as a `ProductSource` and never enters cost/margin math. `POST /inventory/compare/market` gathers it and renders `_market_reference.html` (an HTMX panel under the comparator results); the `Product.upc` barcode (added in migration `h3i4j5k6l7m8`) keys the barcode lookups and is persisted back when the operator enters/corrects it.

`gather_market_reference(query=, upc=, category=)` runs four best-effort fetchers, each degrading to "no data" (never an error) when its key/credential is missing: **UPCitemdb** (barcode → recorded retail band; keyless trial at 100/day, paid key raises it), **Open Prices** (Open Food Facts crowd-sourced shelf prices by barcode), **BLS** (national average for a few categories mapped in `_BLS_SERIES`), and **eBay Browse** (live listings via OAuth client-credentials; flagged `weak=True`). WebstaurantStore and CandyMachines also carry `"weak": True` in `VENDOR_META` so the comparator tags their prices as approximate. Env: `UPCITEMDB_API_KEY` (optional), `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET`, `BLS_API_KEY` (optional) — see `app/config.py`.

UPCs reach products three ways so they don't have to be typed per-SKU: **automatic** — `POST /inventory/upc/fill-missing` resolves blank-UPC active products through `market_reference.search_upc()` (UPCitemdb name search, capped at `_UPC_FILL_LIMIT` per run for the quota, only fills blanks, reviewable); **bulk** — an optional `upc` column on the cost CSV import (UPC-only rows with no price are allowed); **manual** — the `upc` field on the product add/edit form, or the box in the market-reference panel. The barcode lookup itself is fully programmatic: once stored, every market check reuses `Product.upc` automatically.

### Equipment sourcing & curation

The equipment catalog (`/equipment/`) is a procurement tool: each `EquipmentUnit` has many `EquipmentSource` rows (one per `Distributor`), so the team compares prices across suppliers (A&M Equipment Sales, VendGuys, Cantaloupe, etc.). `EquipmentUnit.recompute_best_price()` denormalizes the lowest source price onto `price_low/price_high` for fast catalog rendering; `best_source` drives the "best buy" badge. Units are soft-archived (`status="archived"`), never deleted. `is_locked=True` marks curated/verified rows the AI spec-refresh job must skip (it filters out `is_locked`/archived units) — this stopped the price drift that came from unguarded auto-refreshes. `price_is_starting` renders a "Starting at" label for custom-quoted micro-market packages/kiosks. Catalog data is established by three scripts run in `render.yaml` preDeploy: `seed_distributors.py` (idempotent), `curate_equipment.py` (sentinel-guarded; fixes/archives/sources units + adds the curated lineup), and `fetch_equipment_images.py` (downloads real product photos to slug-named files under `static/images/equipment/`, committed so they survive Render's ephemeral FS).

### Gov Contracts (SAM.gov)

The Lead Gen page (`/leads/`) is a Bootstrap tab shell: **Prospect Research** (the original AI research/email flow) and **Gov Contracts**. The contracts tab is a live search over the public SAM.gov Get Opportunities API (`https://api.sam.gov/opportunities/v2/search`) via `app/services/sam_gov.py`. Like the market-reference fetchers it is **degrade-to-empty**: `search_opportunities(...)` returns `(items, total, reason)` and never raises — `reason` is `"not_configured"` (no `SAM_GOV_API_KEY`), `"unavailable"` (network/rate-limit/non-200), or `""` on success, and the UI renders the matching state. Dates are passed as `YYYY-MM-DD` from HTML date inputs and converted to the API's `MM/dd/yyyy`, defaulting to the last 90 days and clamped to the API's 1-year max span. Defaults are tuned for the business: State `FL`, NAICS `454210` (Vending Machine Operators), with veteran set-asides (SDVOSB/VOSB) surfaced first in the dropdown. The `/leads/contracts/*` routes always return HTMX partials. Operators **Save favorites** to the `saved_contracts` table (`app/models/sam_contract.py`, migration `m8n9o0p1q2r3`); saves are idempotent (upsert by SAM `notice_id`). This is reference/lookup only — it does not feed any AI job or the sales pipeline.

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
# Market-reference pricing (all optional; each source degrades to "no data" when unset)
UPCITEMDB_API_KEY=...
EBAY_CLIENT_ID=...
EBAY_CLIENT_SECRET=...
BLS_API_KEY=...
# SAM.gov contract search (optional; Gov Contracts tab degrades to "not configured" when unset)
SAM_GOV_API_KEY=...
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
- **Staged SEO:** robots.txt / sitemap / canonical / OG / JSON-LD / noindex live on the `feature/seo-enhancements` branch (not merged until ready).
- **One-time data migration:** `scripts/migrate_sqlite_to_postgres.py` (used for the initial SQLite→Postgres cutover).
