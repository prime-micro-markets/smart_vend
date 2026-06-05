from __future__ import annotations

import csv
import io
import json
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func as sql_func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.config import settings as app_settings
from app.database import engine, get_db
from app.models.agent import AgentJob
from app.models.inventory import InventoryLog, Product, ProductSource, Supplier
from app.services import inventory_agent, price_comparator
from app.services.inventory_agent import PRODUCT_CATEGORY_OPTIONS
from app.services.market_reference import gather_market_reference, search_upc
from app.services.price_comparator import (
    COMPARATOR_FETCH_KEYS,
    VENDOR_KEYS,
    load_vendor_settings,
    save_vendor_setting,
)
from app.services.price_fetcher.models import VENDOR_META
from app.services.sams_paste import parse_sams_paste
from app.views import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inventory", tags=["inventory"])

PRODUCT_CATEGORIES = [
    "beverage_water",
    "beverage_energy",
    "beverage_soda",
    "beverage_juice",
    "snack_chips",
    "snack_candy",
    "snack_healthy",
    "meal_sandwich",
    "meal_salad",
    "personal_care",
    "other",
]

# Human-readable labels for internal category slugs
CATEGORY_LABELS: dict[str, str] = {
    "beverage_water": "Water / Still Beverages",
    "beverage_energy": "Energy Drinks",
    "beverage_soda": "Soda / Soft Drinks",
    "beverage_juice": "Juice",
    "snack_chips": "Chips & Crisps",
    "snack_candy": "Candy & Confections",
    "snack_healthy": "Healthy Snacks",
    "meal_sandwich": "Sandwiches & Wraps",
    "meal_salad": "Salads & Fresh Food",
    "personal_care": "Gum, Mints & Personal Care",
    "other": "Other",
}


def _get_setting(db: Session, key: str, default: str = "") -> str:
    from app.models.settings import AppSetting

    row = db.get(AppSetting, key)
    return row.value if row else default


def _set_setting(db: Session, key: str, value: str) -> None:
    from app.models.settings import AppSetting

    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip().replace(",", "").replace("$", "")
    return int(value) if value.lstrip("-").isdigit() else None


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip().replace(",", "").replace("$", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _clean_upc(value: str | None) -> str:
    """Digits-only barcode (drops spaces/dashes from typed or pasted input)."""
    return "".join(ch for ch in (value or "") if ch.isdigit())


_SLUG_BAD_CHARS = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    """Lowercase ASCII-only kebab-case slug. Strips diacritics and punctuation."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_BAD_CHARS.sub("-", normalized.lower()).strip("-")
    return slug or "product"


def _generate_sku(db: Session, name: str, brand: str = "") -> str:
    """Build a unique SKU from product name (+optional brand prefix).

    Used when the operator submits the Add Product form with a blank SKU — the
    goal is to make SKU feel optional in the UI without sacrificing the unique
    handle the rest of the catalog relies on. Appends ``-2``, ``-3`` … on
    collision so the slug stays human-readable.
    """
    base = _slugify(f"{brand} {name}".strip()) if brand else _slugify(name)
    candidate = base[:50]  # keep within the column width (String(50))
    if not db.query(Product.id).filter(Product.sku == candidate).first():
        return candidate
    suffix = 2
    while True:
        # Trim the base so the suffix never pushes us past 50 chars.
        room = 50 - len(f"-{suffix}")
        candidate = f"{base[:room]}-{suffix}"
        if not db.query(Product.id).filter(Product.sku == candidate).first():
            return candidate
        suffix += 1


@router.get("/suppliers", response_class=HTMLResponse)
def suppliers_index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    return templates.TemplateResponse(
        request, "inventory/suppliers.html", {"active_nav": "inventory", "suppliers": suppliers}
    )


_VALID_ACCOUNT_STATUSES = {"not_started", "applied", "open"}


def _normalize_status(value: str | None) -> str:
    v = (value or "").strip().lower().replace(" ", "_")
    return v if v in _VALID_ACCOUNT_STATUSES else "not_started"


@router.post("/suppliers", response_class=HTMLResponse)
def supplier_create(
    name: str = Form(...),
    supplier_type: str = Form(""),
    account_number: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    website: str = Form(""),
    address: str = Form(""),
    account_status: str = Form("not_started"),
    priority: str = Form("100"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    supplier = Supplier(
        name=name,
        supplier_type=supplier_type or None,
        account_number=account_number or None,
        contact_name=contact_name or None,
        contact_email=contact_email or None,
        contact_phone=contact_phone or None,
        website=website or None,
        address=address or None,
        account_status=_normalize_status(account_status),
        priority=_to_int(priority) or 100,
        notes=notes or None,
    )
    try:
        db.add(supplier)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to create supplier %s", name)
        raise HTTPException(status_code=500, detail="Database error saving supplier")
    return RedirectResponse(url="/inventory/?tab=suppliers", status_code=303)


@router.get("/suppliers/{supplier_id}/edit", response_class=HTMLResponse)
def supplier_edit_form(
    supplier_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request, "inventory/supplier_edit.html", {"active_nav": "inventory", "supplier": supplier}
    )


@router.post("/suppliers/{supplier_id}/import", response_class=HTMLResponse)
def supplier_import_prices(
    supplier_id: int,
    request: Request,
    mode: str = Form("csv"),
    payload: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Bulk-import this supplier's price list. See app/services/supplier_import.py."""
    from app.services import supplier_import as si

    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        return Response(status_code=404)

    ctx: dict[str, object] = {
        "active_nav": "inventory",
        "supplier": supplier,
        "submitted_payload": payload,
    }
    if not payload.strip():
        ctx["error"] = "Paste a CSV or catalog text first."
        return templates.TemplateResponse(request, "inventory/supplier_edit.html", ctx)

    try:
        if mode == "ai":
            rows = si.ai_extract_rows(payload)
        else:
            rows = si.parse_csv_text(payload)
    except si.AIExtractError as e:
        ctx["error"] = str(e)
        return templates.TemplateResponse(request, "inventory/supplier_edit.html", ctx)
    except Exception:
        logger.exception("Supplier %d price import failed", supplier_id)
        ctx["error"] = "Unexpected error parsing the payload. Check the server logs."
        return templates.TemplateResponse(request, "inventory/supplier_edit.html", ctx)

    if not rows:
        ctx["error"] = (
            "No rows recognized. CSV mode needs a header row with at least a "
            "`sku` or `name` column. AI mode needs catalog-style text."
        )
        return templates.TemplateResponse(request, "inventory/supplier_edit.html", ctx)

    try:
        result = si.ingest_supplier_offers(db, supplier.id, rows)
    except Exception:
        logger.exception("Supplier %d ingest failed", supplier_id)
        ctx["error"] = "Database error while saving rows. Check the server logs."
        return templates.TemplateResponse(request, "inventory/supplier_edit.html", ctx)

    # Clear the textarea on success — keep the result banner so the operator sees what happened.
    ctx["submitted_payload"] = ""
    ctx["result"] = result
    return templates.TemplateResponse(request, "inventory/supplier_edit.html", ctx)


@router.post("/suppliers/{supplier_id}", response_class=HTMLResponse)
def supplier_update(
    supplier_id: int,
    name: str = Form(...),
    supplier_type: str = Form(""),
    account_number: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    website: str = Form(""),
    address: str = Form(""),
    account_status: str = Form("not_started"),
    priority: str = Form("100"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        return Response(status_code=404)
    supplier.name = name
    supplier.supplier_type = supplier_type or None
    supplier.account_number = account_number or None
    supplier.contact_name = contact_name or None
    supplier.contact_email = contact_email or None
    supplier.contact_phone = contact_phone or None
    supplier.website = website or None
    supplier.address = address or None
    supplier.account_status = _normalize_status(account_status)
    supplier.priority = _to_int(priority) or 100
    supplier.notes = notes or None
    db.commit()
    return RedirectResponse(url="/inventory/?tab=suppliers", status_code=303)


@router.delete("/suppliers/{supplier_id}", response_class=HTMLResponse)
def supplier_delete(supplier_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    supplier = db.get(Supplier, supplier_id)
    if supplier:
        db.delete(supplier)
        db.commit()
    return HTMLResponse(content="", status_code=200)


# Status cycle for the "Open These Accounts" banner. The pill on each row POSTs
# the next status (client-side cycle: not_started → applied → open → not_started)
# and the response is the updated banner partial so the row visibly moves out
# once the account is open.
@router.post("/suppliers/{supplier_id}/status", response_class=HTMLResponse)
def supplier_status_update(
    supplier_id: int,
    request: Request,
    account_status: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        return Response(status_code=404)
    supplier.account_status = _normalize_status(account_status)
    db.commit()
    # Re-render the banner partial with the freshly sorted priority list.
    priority_suppliers = (
        db.query(Supplier)
        .filter(Supplier.account_status != "open", Supplier.priority < 100)
        .order_by(Supplier.priority, Supplier.name)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "inventory/_priority_suppliers.html",
        {"priority_suppliers": priority_suppliers},
    )


@router.get("/", response_class=HTMLResponse)
def inventory_index(
    request: Request,
    category: str | None = None,
    supplier_id: str | None = None,
    low_stock: bool = False,
    seasonal: bool = False,
    init_supplier: str | None = None,
    tab: str = "products",
    product_id: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    sid = int(supplier_id) if supplier_id else None
    init_sid = int(init_supplier) if init_supplier else None
    # Discover Suppliers was merged into Vendors (Inventory v2). Old links and
    # bookmarks targeting ?tab=sourcing or #sourcing still need to land somewhere
    # sensible — they go to the merged Vendors tab where discovery now lives in
    # a collapsed section.
    if tab == "sourcing":
        tab = "suppliers"
    # Eager-load sources (+their suppliers) and the primary supplier so the Best
    # Source column and cost/margin don't fire a query per product (N+1).
    # NOTE: do not shadow the URL `q` parameter — it carries the Find Prices
    # pre-fill query and is read further down.
    products_q = (
        db.query(Product)
        .options(
            selectinload(Product.sources).selectinload(ProductSource.supplier),
            joinedload(Product.primary_supplier),
        )
        .filter(Product.is_active.is_(True))
    )
    if category:
        products_q = products_q.filter(Product.category == category)
    if sid:
        products_q = products_q.filter(Product.primary_supplier_id == sid)
    if low_stock:
        products_q = products_q.filter(
            Product.par_level.is_not(None), Product.on_hand_qty < Product.par_level
        )
    if seasonal:
        products_q = products_q.filter(Product.is_seasonal.is_(True))
    products = products_q.order_by(Product.category, Product.name).all()

    margins = [p.margin_pct for p in products if p.margin_pct is not None]
    summary = {
        "total": len(products),
        "low_stock": sum(1 for p in products if p.is_low_stock),
        "avg_margin": (sum(margins) / len(margins)) if margins else None,
        "missing_cost": sum(1 for p in products if p.effective_cost is None),
        "sourced": sum(1 for p in products if p.source_count),
    }
    # Suppliers tab: open accounts sort first, then by priority, then alpha.
    # The list is small (~24 rows), so in-memory sort beats a multi-column SQL
    # `case()` and lets the priority-section split reuse the same objects.
    suppliers = sorted(
        db.query(Supplier).all(),
        key=lambda s: (s.account_status != "open", s.priority, s.name.lower()),
    )
    # Priority section on the Suppliers tab: not-yet-open suppliers explicitly
    # ranked above the default priority. Lower number sorts to the top.
    priority_suppliers = [s for s in suppliers if s.account_status != "open" and s.priority < 100]
    db_cats = {r[0] for r in db.query(Product.category).distinct() if r[0]}
    categories = PRODUCT_CATEGORIES + [c for c in sorted(db_cats) if c not in PRODUCT_CATEGORIES]
    vendor_cfg = load_vendor_settings(db)
    compare_jobs = (
        db.query(AgentJob)
        .filter(AgentJob.job_type == "price_comparison")
        .order_by(AgentJob.created_at.desc())
        .limit(5)
        .all()
    )
    search_provider = _get_setting(db, "inventory_search_provider", "duckduckgo")
    from app.config import settings as app_settings

    tavily_available = bool(app_settings.tavily_api_key)
    firecrawl_available = bool(app_settings.firecrawl_api_key)
    # Find Prices tab can be pre-bound to a product (clicked from a catalog row
    # or supplier card). The hidden product_id flows through compare_run and the
    # results page so "Save best" doesn't need a second product picker.
    compare_product = None
    compare_product_id = _to_int(product_id)
    if compare_product_id:
        compare_product = db.get(Product, compare_product_id)
    sourcing_jobs = (
        db.query(AgentJob)
        .filter(AgentJob.job_type == "inventory_research")
        .order_by(AgentJob.created_at.desc())
        .limit(20)
        .all()
    )
    # Bulk "Source all missing prices" surfaces only when there's work to do.
    # Counted on the already-loaded products so we don't hit the DB twice.
    unsourced_count = sum(1 for p in products if not p.source_count)
    bulk_sourcing_active = _get_setting(db, "bulk_sourcing_in_progress", "") == "1"
    return templates.TemplateResponse(
        request,
        "inventory/index.html",
        {
            "active_nav": "inventory",
            "products": products,
            "suppliers": suppliers,
            "priority_suppliers": priority_suppliers,
            "categories": categories,
            "category_labels": CATEGORY_LABELS,
            "category_filter": category,
            "supplier_filter": sid,
            "low_stock": low_stock,
            "seasonal_filter": seasonal,
            "summary": summary,
            "init_supplier_id": init_sid,
            "active_tab": tab,
            # comparator
            "vendor_keys": VENDOR_KEYS,
            # Vendors actually dispatched as checkboxes (Sam's is ingest-only).
            "compare_vendor_keys": COMPARATOR_FETCH_KEYS,
            "vendor_meta": VENDOR_META,
            "vendor_cfg": vendor_cfg,
            "compare_jobs": compare_jobs,
            "search_provider": search_provider,
            "tavily_available": tavily_available,
            "firecrawl_available": firecrawl_available,
            "compare_product": compare_product,
            "compare_query": (q or (compare_product.name if compare_product else "")),
            # AI sourcing
            "category_options": PRODUCT_CATEGORY_OPTIONS,
            "current_provider": search_provider,
            "sourcing_jobs": sourcing_jobs,
            # Bulk sourcing (v2)
            "unsourced_count": unsourced_count,
            "bulk_sourcing_active": bulk_sourcing_active,
        },
    )


@router.get("/search/usage", response_class=HTMLResponse)
def inventory_search_usage(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    today_start = datetime.combine(date.today(), datetime.min.time())
    today_tokens = (
        db.query(sql_func.sum(AgentJob.tokens_used))
        .filter(AgentJob.job_type == "inventory_research", AgentJob.created_at >= today_start)
        .scalar()
        or 0
    )
    total_tokens = (
        db.query(sql_func.sum(AgentJob.tokens_used))
        .filter(AgentJob.job_type == "inventory_research")
        .scalar()
        or 0
    )
    latest = (
        db.query(AgentJob)
        .filter(
            AgentJob.job_type == "inventory_research",
            AgentJob.ratelimit_tokens_remaining.isnot(None),
        )
        .order_by(AgentJob.finished_at.desc())
        .first()
    )
    return templates.TemplateResponse(
        request,
        "leads/_usage_widget.html",
        {"today_tokens": today_tokens, "total_tokens": total_tokens, "latest_job": latest},
    )


@router.get("/search", response_class=HTMLResponse)
def inventory_search_index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    jobs = (
        db.query(AgentJob)
        .filter(AgentJob.job_type == "inventory_research")
        .order_by(AgentJob.created_at.desc())
        .limit(100)
        .all()
    )
    current_provider = _get_setting(db, "inventory_search_provider", "duckduckgo")
    return templates.TemplateResponse(
        request,
        "inventory/search.html",
        {
            "active_nav": "inventory",
            "jobs": jobs,
            "category_options": PRODUCT_CATEGORY_OPTIONS,
            "current_provider": current_provider,
        },
    )


@router.post("/search", response_class=HTMLResponse)
def inventory_search_run(
    request: Request,
    background_tasks: BackgroundTasks,
    product_categories: list[str] = Form(default=[]),
    location_city: str = Form("Panama City"),
    location_state: str = Form("FL"),
    search_focus: str = Form(""),
    max_results: int = Form(15),
    search_provider: str = Form("duckduckgo"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    max_results = max(1, min(50, max_results))
    _set_setting(db, "inventory_search_provider", search_provider)

    # Auto-reset stale jobs stuck for over 2 hours
    stale_cutoff = datetime.now() - timedelta(hours=2)
    db.query(AgentJob).filter(
        AgentJob.job_type == "inventory_research",
        AgentJob.status.in_(["running", "pending"]),
        AgentJob.created_at < stale_cutoff,
    ).update({"status": "error", "error_message": "Auto-reset: exceeded 2-hour limit"})
    db.commit()

    location = f"{location_city.strip()}, {location_state.strip()}"
    params = {
        "product_categories": product_categories,
        "location": location,
        "search_focus": search_focus.strip(),
        "max_results": max_results,
        "search_provider": search_provider,
    }
    job = AgentJob(
        job_type="inventory_research",
        status="pending",
        input_params=json.dumps(params),
    )
    try:
        db.add(job)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to create inventory search job")
        raise HTTPException(status_code=500, detail="Database error creating job")
    background_tasks.add_task(inventory_agent.run_inventory_search_job, job.id)
    return RedirectResponse(url=f"/inventory/search/{job.id}", status_code=303)


@router.get("/search/jobs", response_class=HTMLResponse)
def inventory_search_jobs_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    jobs = (
        db.query(AgentJob)
        .filter(AgentJob.job_type == "inventory_research")
        .order_by(AgentJob.created_at.desc())
        .limit(20)
        .all()
    )
    return templates.TemplateResponse(request, "inventory/_search_job_history.html", {"jobs": jobs})


@router.get("/search/{job_id}/poll", response_class=HTMLResponse)
def inventory_search_poll(
    job_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    job = db.get(AgentJob, job_id)
    if not job:
        return HTMLResponse(content="<div>Job not found</div>", status_code=404)
    supplier_results: list[dict] = []
    if job.status == "done" and job.draft_body:
        try:
            supplier_results = json.loads(job.draft_body)
        except Exception:
            supplier_results = []
    # Build a set of existing supplier names for status badges
    existing_names: set[str] = set()
    if supplier_results:
        names = [s.get("supplier_name", "").strip().lower() for s in supplier_results]
        rows = db.query(Supplier.name).filter(Supplier.name.in_([n for n in names if n])).all()
        existing_names = {r[0].lower() for r in rows}
    return templates.TemplateResponse(
        request,
        "inventory/_search_job_status_card.html",
        {"job": job, "supplier_results": supplier_results, "existing_names": existing_names},
    )


@router.get("/search/{job_id}", response_class=HTMLResponse)
def inventory_search_job(
    job_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    job = db.get(AgentJob, job_id)
    if not job:
        return Response(status_code=404)
    supplier_results: list[dict] = []
    if job.draft_body:
        try:
            supplier_results = json.loads(job.draft_body)
        except Exception:
            supplier_results = []
    existing_names: set[str] = set()
    supplier_id_map: dict[str, int] = {}
    if supplier_results:
        names = [s.get("supplier_name", "").strip().lower() for s in supplier_results]
        rows = (
            db.query(Supplier.id, Supplier.name)
            .filter(Supplier.name.in_([n for n in names if n]))
            .all()
        )
        existing_names = {r[1].lower() for r in rows}
        supplier_id_map = {r[1].lower(): r[0] for r in rows}
    input_params: dict = {}
    if job.input_params:
        try:
            input_params = json.loads(job.input_params)
        except Exception:
            pass
    return templates.TemplateResponse(
        request,
        "inventory/search_job.html",
        {
            "active_nav": "inventory",
            "job": job,
            "supplier_results": supplier_results,
            "existing_names": existing_names,
            "supplier_id_map": supplier_id_map,
            "input_params": input_params,
        },
    )


@router.post("/search/{job_id}/delete", response_class=HTMLResponse)
def inventory_search_delete(job_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.get(AgentJob, job_id)
    if job and job.job_type == "inventory_research":
        db.delete(job)
        db.commit()
    return HTMLResponse(content="", status_code=200)


@router.get("/new", response_class=HTMLResponse)
def product_new_form(
    request: Request, supplier_id: int | None = None, db: Session = Depends(get_db)
) -> HTMLResponse:
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    return templates.TemplateResponse(
        request,
        "inventory/_product_form.html",
        {"product": None, "suppliers": suppliers, "selected_supplier_id": supplier_id},
    )


@router.post("/", response_class=HTMLResponse)
def product_create(
    request: Request,
    name: str = Form(...),
    sku: str = Form(""),
    brand: str = Form(""),
    category: str = Form(""),
    upc: str = Form(""),
    unit_cost: str = Form(""),
    sell_price: str = Form(""),
    unit_size: str = Form(""),
    case_pack_qty: str = Form(""),
    par_level: str = Form(""),
    is_seasonal: bool = Form(False),
    primary_supplier_id: str = Form(""),
    restock_notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        parsed_cost = float(unit_cost) if unit_cost else None
        parsed_sell = float(sell_price) if sell_price else None
        parsed_pack = int(case_pack_qty) if case_pack_qty else None
        parsed_par = int(par_level) if par_level else None
        parsed_supplier = int(primary_supplier_id) if primary_supplier_id else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid numeric value: {exc}") from exc

    if parsed_cost is not None and parsed_cost < 0:
        raise HTTPException(status_code=422, detail="Unit cost cannot be negative")
    if parsed_par is not None and parsed_par < 0:
        raise HTTPException(status_code=422, detail="Par level cannot be negative")

    # Blank SKU → derive from the name (+ brand prefix when set). Lets the form
    # ask for one less thing without giving up the column's uniqueness guarantee.
    resolved_sku = sku.strip() or _generate_sku(db, name, brand)

    product = Product(
        sku=resolved_sku,
        name=name,
        brand=brand or None,
        category=category or None,
        upc=_clean_upc(upc) or None,
        unit_cost=parsed_cost,
        sell_price=parsed_sell,
        unit_size=unit_size or None,
        case_pack_qty=parsed_pack,
        par_level=parsed_par,
        is_seasonal=is_seasonal,
        primary_supplier_id=parsed_supplier,
        restock_notes=restock_notes or None,
    )
    try:
        db.add(product)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to create product %s", resolved_sku)
        raise HTTPException(status_code=500, detail="Database error saving product")
    return RedirectResponse(url="/inventory/", status_code=303)


@router.post("/seed-starter", response_class=HTMLResponse)
def seed_starter_products(db: Session = Depends(get_db)) -> HTMLResponse:
    """Seed ~25 common vending SKUs from the empty-state onboarding card.

    Unlike the CLI script, this path ignores the seed sentinel — the operator
    clicked the button deliberately, so re-running on demand (e.g. after a
    catalog wipe) is the intended behavior. Existing rows are never clobbered;
    only blank fields are backfilled.
    """
    from app.services.starter_catalog import ensure_starter_products

    created, backfilled = ensure_starter_products(db)
    db.commit()
    logger.info("Starter products seeded: %d created, %d backfilled", created, backfilled)
    return RedirectResponse(url="/inventory/", status_code=303)


# ---------------------------------------------------------------------------
# Price Comparator routes  (must be before /{product_id} wildcard routes)
# ---------------------------------------------------------------------------


@router.get("/compare/stores", response_class=HTMLResponse)
def compare_stores(
    request: Request,
    brand: str = Query("sams_club"),
    compare_sams_zip: str = Query(""),
) -> HTMLResponse:
    """Return nearby Sam's Club list HTML for the store picker in settings."""
    from app.services import store_locator

    zip_code = compare_sams_zip.strip()
    stores: list[dict] = []
    error: str | None = None

    if zip_code:
        try:
            stores = store_locator.find_stores(zip_code, brand)
        except Exception as exc:
            error = str(exc)
    else:
        error = "Enter a ZIP code first."

    return templates.TemplateResponse(
        request,
        "inventory/_store_picker.html",
        {
            "stores": stores,
            "brand": brand,
            "zip_code": zip_code,
            "error": error,
        },
    )


@router.post("/compare/settings", response_class=HTMLResponse)
def compare_settings_save(
    request: Request,
    compare_sams_zip: str = Form(""),
    compare_sams_club_id: str = Form(""),
    compare_sams_club_name: str = Form(""),
    compare_webstaurantstore_email: str = Form(""),
    compare_candy_machines_email: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Save vendor account config."""
    settings_to_save: dict[str, str] = {
        "compare_webstaurantstore_email": compare_webstaurantstore_email.strip(),
        "compare_candy_machines_email": compare_candy_machines_email.strip(),
        "compare_sams_zip": compare_sams_zip.strip(),
    }

    sams_club_id = compare_sams_club_id.strip()
    if sams_club_id:
        settings_to_save["compare_sams_club_id"] = sams_club_id
        if compare_sams_club_name.strip():
            settings_to_save["compare_sams_club_name"] = compare_sams_club_name.strip()

    for key, value in settings_to_save.items():
        if value:
            save_vendor_setting(db, key, value)

    # Redirect so the full page reloads with updated vendor badges in the search form
    from starlette.responses import Response as StarletteResponse

    resp = StarletteResponse(status_code=200)
    resp.headers["HX-Redirect"] = "/inventory/?tab=compare"
    return resp


@router.post("/compare", response_class=HTMLResponse)
def compare_run(
    request: Request,
    background_tasks: BackgroundTasks,
    product_query: str = Form(...),
    vendors: list[str] = Form(default=[]),
    search_provider: str = Form("duckduckgo"),
    product_id: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    vendor_cfg = load_vendor_settings(db)
    # Length cap defends against accidental Query/object stringification leaking
    # into the form (Tavily caps at 400 chars and a giant query is never a real
    # SKU search anyway).
    cleaned_query = product_query.strip()[:200]
    bound_pid = _to_int(product_id)
    bound_pack: int | None = None
    if bound_pid:
        bound_product = db.get(Product, bound_pid)
        if bound_product and bound_product.case_pack_qty:
            bound_pack = bound_product.case_pack_qty
    params = {
        "product_query": cleaned_query,
        "vendors": vendors or COMPARATOR_FETCH_KEYS,
        "search_provider": search_provider,
        "vendor_config": vendor_cfg,
        # When launched from a product detail page, results can be saved back as
        # ProductSource rows for that SKU (see /sources/from-comparison).
        "product_id": bound_pid,
        # Lets the dispatcher backfill case_price from retail unit_price for
        # cross-vendor comparison (Inventory v2 normalize() pass).
        "fallback_case_pack": bound_pack,
    }
    job = AgentJob(
        job_type="price_comparison",
        status="pending",
        input_params=json.dumps(params),
    )
    db.add(job)
    db.commit()
    background_tasks.add_task(price_comparator.run_price_comparison_job, job.id)
    return RedirectResponse(url=f"/inventory/compare/{job.id}", status_code=303)


@router.get("/compare/history", response_class=HTMLResponse)
def compare_history(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    jobs = (
        db.query(AgentJob)
        .filter(AgentJob.job_type == "price_comparison")
        .order_by(AgentJob.created_at.desc())
        .limit(10)
        .all()
    )
    return templates.TemplateResponse(request, "inventory/_comparator_history.html", {"jobs": jobs})


@router.get("/compare/{job_id}/poll", response_class=HTMLResponse)
def compare_poll(job_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.get(AgentJob, job_id)
    if not job:
        return HTMLResponse(content="<div>Job not found</div>", status_code=404)
    results: list[dict] = []
    if job.status == "done" and job.draft_body:
        try:
            results = json.loads(job.draft_body)
            results.sort(key=lambda r: (r.get("unit_price") is None, r.get("unit_price") or 0))
        except Exception:
            pass
    input_params: dict = {}
    if job.input_params:
        try:
            input_params = json.loads(job.input_params)
        except Exception:
            pass
    target_product = None
    if input_params.get("product_id"):
        target_product = db.get(Product, input_params["product_id"])
    network_blocked = bool(price_comparator.job_summary(job).get("network_blocked"))
    return templates.TemplateResponse(
        request,
        "inventory/_comparator_poll.html",
        {
            "job": job,
            "results": results,
            "input_params": input_params,
            "vendor_meta": VENDOR_META,
            "target_product": target_product,
            "network_blocked": network_blocked,
        },
    )


@router.get("/compare/{job_id}", response_class=HTMLResponse)
def compare_results(job_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.get(AgentJob, job_id)
    if not job:
        return Response(status_code=404)
    results: list[dict] = []
    if job.draft_body:
        try:
            results = json.loads(job.draft_body)
            results.sort(key=lambda r: (r.get("unit_price") is None, r.get("unit_price") or 0))
        except Exception:
            pass
    input_params: dict = {}
    if job.input_params:
        try:
            input_params = json.loads(job.input_params)
        except Exception:
            pass
    target_product = None
    if input_params.get("product_id"):
        target_product = db.get(Product, input_params["product_id"])
    network_blocked = bool(price_comparator.job_summary(job).get("network_blocked"))
    return templates.TemplateResponse(
        request,
        "inventory/compare_results.html",
        {
            "active_nav": "inventory",
            "job": job,
            "results": results,
            "input_params": input_params,
            "vendor_meta": VENDOR_META,
            "target_product": target_product,
            "network_blocked": network_blocked,
        },
    )


@router.post("/compare/{job_id}/delete", response_class=HTMLResponse)
def compare_delete(job_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.get(AgentJob, job_id)
    if job and job.job_type == "price_comparison":
        db.delete(job)
        db.commit()
    return HTMLResponse(content="", status_code=200)


# ---------------------------------------------------------------------------
# Restock Run — shopping list of below-par SKUs grouped by cheapest supplier
# (fixed path — must be declared before the /{product_id} wildcard below)
# ---------------------------------------------------------------------------


@router.get("/restock-run", response_class=HTMLResponse)
def restock_run(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    import math

    products = (
        db.query(Product)
        .options(
            selectinload(Product.sources).selectinload(ProductSource.supplier),
            joinedload(Product.primary_supplier),
        )
        .filter(
            Product.is_active.is_(True),
            Product.par_level.is_not(None),
            Product.on_hand_qty < Product.par_level,
        )
        .order_by(Product.category, Product.name)
        .all()
    )

    groups: dict[str, dict] = {}
    grand_total = 0.0
    for p in products:
        src = p.buying_source
        supplier_name = (
            src.supplier.name
            if src
            else (p.primary_supplier.name if p.primary_supplier else "Unassigned")
        )
        qty = p.qty_needed
        unit_cost = src.effective_unit_cost if src else p.effective_cost
        case_price = src.case_price if src else None
        pack = (src.case_pack_qty if src else None) or p.case_pack_qty
        cases = None
        line_cost = None
        if case_price and pack:
            cases = math.ceil(qty / pack)
            line_cost = cases * case_price
        elif unit_cost is not None:
            line_cost = qty * unit_cost

        grp = groups.setdefault(
            supplier_name,
            {"supplier_name": supplier_name, "lines": [], "subtotal": 0.0, "has_unknown": False},
        )
        grp["lines"].append(
            {
                "product": p,
                "qty_needed": qty,
                "source": src,
                "unit_cost": unit_cost,
                "case_price": case_price,
                "pack": pack,
                "cases": cases,
                "line_cost": line_cost,
                "in_stock": src.in_stock if src else False,
                "supplier_url": src.supplier_url if src else None,
            }
        )
        if line_cost is not None:
            grp["subtotal"] += line_cost
            grand_total += line_cost
        else:
            grp["has_unknown"] = True

    # Largest supplier orders first (tackle the big buys first); items with no
    # known supplier ("Unassigned") sink to the bottom.
    ordered = sorted(
        groups.values(),
        key=lambda g: (g["supplier_name"] == "Unassigned", -g["subtotal"], g["supplier_name"]),
    )
    return templates.TemplateResponse(
        request,
        "inventory/restock_run.html",
        {
            "active_nav": "inventory",
            "groups": ordered,
            "grand_total": grand_total,
            "total_skus": len(products),
            "category_labels": CATEGORY_LABELS,
        },
    )


# ---------------------------------------------------------------------------
# Product wildcard routes  (must be after all fixed-path routes above)
# ---------------------------------------------------------------------------


@router.get("/{product_id}/edit", response_class=HTMLResponse)
def product_edit_form(
    product_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    product = db.get(Product, product_id)
    if not product:
        return Response(status_code=404)
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    return templates.TemplateResponse(
        request, "inventory/_product_form.html", {"product": product, "suppliers": suppliers}
    )


@router.post("/{product_id}", response_class=HTMLResponse)
def product_update(
    product_id: int,
    name: str = Form(...),
    brand: str = Form(""),
    category: str = Form(""),
    upc: str = Form(""),
    unit_cost: str = Form(""),
    sell_price: str = Form(""),
    unit_size: str = Form(""),
    case_pack_qty: str = Form(""),
    par_level: str = Form(""),
    is_seasonal: bool = Form(False),
    primary_supplier_id: str = Form(""),
    restock_notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    product = db.get(Product, product_id)
    if not product:
        return Response(status_code=404)
    product.name = name
    product.brand = brand or None
    product.category = category or None
    product.upc = _clean_upc(upc) or None
    product.unit_cost = float(unit_cost) if unit_cost else None
    product.sell_price = float(sell_price) if sell_price else None
    product.unit_size = unit_size or None
    product.case_pack_qty = int(case_pack_qty) if case_pack_qty else None
    product.par_level = int(par_level) if par_level else None
    product.is_seasonal = is_seasonal
    product.primary_supplier_id = int(primary_supplier_id) if primary_supplier_id else None
    product.restock_notes = restock_notes or None
    db.commit()
    return RedirectResponse(url="/inventory/", status_code=303)


@router.post("/{product_id}/restock", response_class=HTMLResponse)
def product_restock(
    request: Request,
    product_id: int,
    qty: int = Form(...),
    notes: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    product = db.get(Product, product_id)
    if not product:
        return Response(status_code=404)
    product.on_hand_qty += qty
    user = request.session.get("user") or {}
    log = InventoryLog(
        product_id=product_id,
        log_type="restock",
        qty_change=qty,
        qty_after=product.on_hand_qty,
        unit_cost_at_log=product.effective_cost,
        notes=notes or None,
        logged_by=user.get("email") or user.get("name"),
    )
    db.add(log)
    db.commit()
    dest = next if next.startswith("/inventory/") else "/inventory/"
    return RedirectResponse(url=dest, status_code=303)


@router.delete("/{product_id}", response_class=HTMLResponse)
def product_deactivate(product_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    product = db.get(Product, product_id)
    if product:
        product.is_active = False
        db.commit()
    return HTMLResponse(content="", status_code=200)


# ---------------------------------------------------------------------------
# Product detail page + per-supplier sourcing  (wildcard — keep after the above)
# ---------------------------------------------------------------------------


def _get_product_or_404(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


def _sources_partial(request: Request, product: Product, db: Session) -> HTMLResponse:
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    return templates.TemplateResponse(
        request,
        "inventory/_product_sources.html",
        {"product": product, "suppliers": suppliers},
    )


@router.get("/{product_id}", response_class=HTMLResponse)
def product_detail(
    product_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    product = _get_product_or_404(db, product_id)
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    logs = (
        db.query(InventoryLog)
        .filter(InventoryLog.product_id == product_id)
        .order_by(InventoryLog.logged_at.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "inventory/detail.html",
        {
            "active_nav": "inventory",
            "product": product,
            "suppliers": suppliers,
            "logs": logs,
            "category_labels": CATEGORY_LABELS,
        },
    )


@router.post("/{product_id}/sources", response_class=HTMLResponse)
def add_product_source(
    request: Request,
    product_id: int,
    supplier_id: str = Form(""),
    new_supplier_name: str = Form(""),
    supplier_url: str = Form(""),
    case_price: str = Form(""),
    case_pack_qty: str = Form(""),
    unit_cost: str = Form(""),
    unit_size: str = Form(""),
    min_order: str = Form(""),
    price_notes: str = Form(""),
    in_stock: bool = Form(False),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    product = _get_product_or_404(db, product_id)

    sup_id = _to_int(supplier_id)
    if not sup_id and new_supplier_name.strip():
        # Let the team add a supplier on the fly without leaving the form.
        existing = db.query(Supplier).filter(Supplier.name == new_supplier_name.strip()).first()
        if existing:
            sup_id = existing.id
        else:
            supplier = Supplier(name=new_supplier_name.strip())
            db.add(supplier)
            db.flush()
            sup_id = supplier.id
    if not sup_id:
        raise HTTPException(status_code=400, detail="A supplier is required")

    # Upsert: re-saving an existing supplier's price updates it rather than adding
    # a duplicate row for the same (product, supplier).
    source = next((s for s in product.sources if s.supplier_id == sup_id), None)
    if source is None:
        source = ProductSource(product_id=product.id, supplier_id=sup_id, origin="manual")
        db.add(source)
    source.supplier_url = supplier_url.strip() or None
    source.case_price = _to_float(case_price)
    source.case_pack_qty = _to_int(case_pack_qty) or product.case_pack_qty
    source.unit_cost = _to_float(unit_cost)
    source.unit_size = unit_size.strip() or None
    source.min_order = min_order.strip() or None
    source.price_notes = price_notes.strip() or None
    source.in_stock = in_stock
    source.last_verified = datetime.now()
    db.commit()
    db.refresh(product)
    return _sources_partial(request, product, db)


@router.post("/{product_id}/sources/{source_id}/delete", response_class=HTMLResponse)
def delete_product_source(
    request: Request, product_id: int, source_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    product = _get_product_or_404(db, product_id)
    source = db.get(ProductSource, source_id)
    if source and source.product_id == product.id:
        db.delete(source)
        db.commit()
        db.refresh(product)
    return _sources_partial(request, product, db)


@router.post("/{product_id}/sources/{source_id}/preferred", response_class=HTMLResponse)
def toggle_preferred_product_source(
    request: Request, product_id: int, source_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    product = _get_product_or_404(db, product_id)
    source = db.get(ProductSource, source_id)
    if source and source.product_id == product.id:
        # Only one preferred source per product.
        for s in product.sources:
            s.is_preferred = s.id == source_id and not source.is_preferred
        db.commit()
        db.refresh(product)
    return _sources_partial(request, product, db)


def _upsert_product_source_from_comparison(
    db: Session,
    product: Product,
    *,
    vendor_name: str,
    vendor_key: str = "",
    unit_price: float | None = None,
    case_price: float | None = None,
    case_qty: int | None = None,
    unit_size: str | None = None,
    url: str | None = None,
    notes: str | None = None,
    origin: str = "comparator",
) -> ProductSource:
    """Upsert a ProductSource from a comparator result row.

    Shared by both the form-driven "Save to product" button and the v2
    background flows (per-row refresh, "Source all missing prices"). The
    vendor name maps to a Supplier (found by lowercase match, else created);
    the source row is keyed on (product, supplier) so re-saving the same
    vendor refreshes price rather than duplicating rows.

    ``origin`` distinguishes saves: ``"comparator"`` (operator clicked save),
    ``"comparator_auto"`` (bulk/per-row background auto-save), or
    ``"comparator_discovered"`` (off-known-vendor host saved from the CM
    referrals or AI fallback subsection). Backfills missing unit math from
    case math so the saved row is consistent.
    """
    supplier = (
        db.query(Supplier).filter(sql_func.lower(Supplier.name) == vendor_name.lower()).first()
    )
    if not supplier:
        meta = VENDOR_META.get(vendor_key, {})
        supplier = Supplier(name=vendor_name, supplier_type=meta.get("type") or "online")
        db.add(supplier)
        db.flush()

    source = next((s for s in product.sources if s.supplier_id == supplier.id), None)
    if source is None:
        source = ProductSource(product_id=product.id, supplier_id=supplier.id, origin=origin)
        db.add(source)
    else:
        # Preserve a more specific origin (e.g. "comparator_discovered") if it
        # was set previously, but upgrade a stale "comparator" to "auto" when
        # the background path refreshed it.
        if origin and source.origin != origin and not source.origin:
            source.origin = origin
        elif origin == "comparator_auto" and source.origin == "comparator":
            source.origin = "comparator_auto"

    # Backfill missing unit math so the persisted row is internally consistent.
    pack = case_qty or product.case_pack_qty
    if unit_price is None and case_price is not None and pack:
        unit_price = round(case_price / pack, 4)
    if case_price is None and unit_price is not None and pack:
        case_price = round(unit_price * pack, 2)

    source.supplier_url = url or None
    source.case_price = case_price
    source.case_pack_qty = pack
    source.unit_cost = unit_price
    source.unit_size = unit_size or None
    source.price_notes = notes or None
    source.last_verified = datetime.now()
    return source


@router.post("/{product_id}/sources/from-comparison", response_class=HTMLResponse)
def save_source_from_comparison(
    product_id: int,
    vendor_name: str = Form(...),
    vendor_key: str = Form(""),
    unit_price: str = Form(""),
    case_price: str = Form(""),
    case_qty: str = Form(""),
    unit_size: str = Form(""),
    url: str = Form(""),
    notes: str = Form(""),
    # v2: lets the off-domain "Discovered via web search" save flow tag the
    # ProductSource so Vendors-tab chips can distinguish the origin.
    origin: str = Form("comparator"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Persist one Find Product Prices result as a ProductSource for this SKU."""
    product = _get_product_or_404(db, product_id)
    name = vendor_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Vendor name required")

    _upsert_product_source_from_comparison(
        db,
        product,
        vendor_name=name,
        vendor_key=vendor_key,
        unit_price=_to_float(unit_price),
        case_price=_to_float(case_price),
        case_qty=_to_int(case_qty),
        unit_size=unit_size.strip(),
        url=url.strip(),
        notes=notes.strip(),
        origin=origin or "comparator",
    )
    db.commit()
    return HTMLResponse(
        '<span class="badge bg-success"><i class="bi bi-check-lg me-1"></i>Saved to '
        f'<a href="/inventory/{product.id}" class="text-white text-decoration-underline">product</a></span>'
    )


# ----------------------------------------------------------------------
# Phase 1: fast cost entry (bulk grid + CSV import)
# ----------------------------------------------------------------------
# The live comparator can't reliably populate costs (Sam's is Akamai-blocked,
# WebstaurantStore needs a Firecrawl key), so the operator was left with 26 SKUs
# and 26 blank costs. These two paths let them enter real costs in minutes: an
# inline bulk grid and a CSV import. Both upsert a ProductSource (default Sam's
# Club, since that's where the business actually buys) so the saved cost flows
# straight into the comparator's best-source / margin math.

_BULK_COST_ORIGIN = "bulk_entry"


def _supplier_vendor_key(supplier_name: str) -> str:
    """Map a supplier name back to a known vendor_key so the auto-created
    Supplier picks up the right type/icon (Sam's Club → local_wholesale)."""
    return "sams_club" if supplier_name.strip().lower() == "sam's club" else ""


@router.post("/costs/bulk")
async def bulk_costs_save(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """Save the inline bulk-cost grid: one ProductSource per filled row."""
    form = await request.form()
    supplier_name = (str(form.get("supplier_name") or "Sam's Club")).strip() or "Sam's Club"
    vendor_key = _supplier_vendor_key(supplier_name)

    # Field names are case_price_<id> / pack_<id>; collect the ids present.
    pids = {k[len("case_price_") :] for k in form if k.startswith("case_price_")}
    saved = 0
    for pid_str in pids:
        pid = _to_int(pid_str)
        if not pid:
            continue
        case_price = _to_float(str(form.get(f"case_price_{pid_str}") or ""))
        if case_price is None or case_price <= 0:
            continue  # only filled rows save
        pack = _to_int(str(form.get(f"pack_{pid_str}") or ""))
        product = db.get(Product, pid)
        if not product:
            continue
        _upsert_product_source_from_comparison(
            db,
            product,
            vendor_name=supplier_name,
            vendor_key=vendor_key,
            case_price=case_price,
            case_qty=pack,
            notes="Bulk cost entry",
            origin=_BULK_COST_ORIGIN,
        )
        # Backfill the product's case pack so future rows pre-fill correctly.
        if pack and not product.case_pack_qty:
            product.case_pack_qty = pack
        saved += 1

    db.commit()
    return RedirectResponse(url=f"/inventory/?tab=products&costs_saved={saved}", status_code=303)


# upc is last so it's optional and a bare cost file (sku,case_price,case_pack_qty)
# still validates; when present it bulk-populates Product.upc for market lookups.
_COST_CSV_HEADERS = ["sku", "case_price", "case_pack_qty", "upc"]


@router.get("/costs/import/template")
def cost_import_template() -> Response:
    """Download a starter CSV the operator can fill in or adapt from a Sam's export."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_COST_CSV_HEADERS)
    writer.writerow(["WATER-16OZ", "18.96", "24", "012000161155"])
    writer.writerow(["COKE-12OZ", "13.48", "32", ""])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=cost_import_template.csv"},
    )


@router.get("/costs/import", response_class=HTMLResponse)
def cost_import_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Upload form for the cost CSV import."""
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    return templates.TemplateResponse(
        request,
        "inventory/cost_import.html",
        {"active_nav": "inventory", "suppliers": suppliers},
    )


@router.post("/costs/import", response_class=HTMLResponse)
async def cost_import_run(
    request: Request,
    file: UploadFile = File(...),
    supplier_name: str = Form("Sam's Club"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Parse an uploaded cost CSV and upsert a ProductSource per matched SKU.

    Matches by SKU (case-insensitive) first, then exact product name. Rows that
    don't match or lack a positive case price are reported back, not silently
    dropped, so the operator can fix and re-upload.
    """
    supplier_name = supplier_name.strip() or "Sam's Club"
    vendor_key = _supplier_vendor_key(supplier_name)

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    # Normalize headers to lowercase/stripped so a Sam's-style export with
    # capitalized columns still maps onto sku/case_price/case_pack_qty.
    field_map = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}

    def cell(row: dict, key: str) -> str:
        src = field_map.get(key)
        return (row.get(src) or "").strip() if src else ""

    # Index products once for fast lookup by sku and by name.
    products = db.query(Product).all()
    by_sku = {p.sku.lower(): p for p in products}
    by_name = {p.name.lower(): p for p in products}

    saved = 0
    upcs_set = 0
    unmatched: list[str] = []
    skipped: list[str] = []
    for row in reader:
        sku = cell(row, "sku")
        name = cell(row, "name")
        key = (sku or name).strip()
        if not key:
            continue
        product = by_sku.get(sku.lower()) or by_name.get(name.lower())
        if not product:
            unmatched.append(key)
            continue
        # A barcode column populates Product.upc in the same pass — set it even
        # on price-less rows so a UPC-only file is a valid bulk barcode import.
        upc = _clean_upc(cell(row, "upc"))
        if upc and product.upc != upc:
            product.upc = upc
            upcs_set += 1
        case_price = _to_float(cell(row, "case_price"))
        if case_price is None or case_price <= 0:
            if not upc:  # row did nothing at all
                skipped.append(key)
            continue
        pack = _to_int(cell(row, "case_pack_qty"))
        _upsert_product_source_from_comparison(
            db,
            product,
            vendor_name=supplier_name,
            vendor_key=vendor_key,
            case_price=case_price,
            case_qty=pack,
            notes="CSV cost import",
            origin=_BULK_COST_ORIGIN,
        )
        if pack and not product.case_pack_qty:
            product.case_pack_qty = pack
        saved += 1

    db.commit()
    return templates.TemplateResponse(
        request,
        "inventory/cost_import_result.html",
        {
            "active_nav": "inventory",
            "saved": saved,
            "upcs_set": upcs_set,
            "unmatched": unmatched,
            "skipped": skipped,
            "supplier_name": supplier_name,
        },
    )


# ----------------------------------------------------------------------
# Sam's Club purchase-history paste import
# ----------------------------------------------------------------------
# Sam's blocks server-side price pulls, so actual paid prices come from the
# operator's own logged-in browser: a no-token bookmarklet on samsclub.com/orders
# copies the line items to the clipboard, the operator pastes them here (behind
# the normal Google login), reviews/matches each row to a SKU, and saves the
# real cost. This replaced the cross-origin /ingest token + Playwright plumbing.

_SAMS_PASTE_ORIGIN = "sams_purchase"

_NAME_STOPWORDS = frozenset(
    {"oz", "ct", "pk", "pack", "count", "fl", "case", "the", "of", "and", "with"}
)


def _name_tokens(name: str) -> set[str]:
    """Normalized word set for fuzzy product matching (lowercase, alnum, no units)."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {w for w in words if w not in _NAME_STOPWORDS and not w.isdigit()}


def _best_product_match(line_name: str, products: list[Product]) -> Product | None:
    """Pick the catalog product whose name shares the most meaningful tokens with
    the pasted line. Returns None when nothing overlaps (operator picks manually)."""
    target = _name_tokens(line_name)
    if not target:
        return None
    best: Product | None = None
    best_score = 0
    for p in products:
        overlap = len(target & _name_tokens(p.name))
        if overlap > best_score:
            best, best_score = p, overlap
    return best if best_score > 0 else None


@router.get("/costs/sams-paste", response_class=HTMLResponse)
def sams_paste_page(request: Request) -> HTMLResponse:
    """Show the paste box + bookmarklet for importing Sam's purchase history."""
    return templates.TemplateResponse(
        request,
        "inventory/sams_paste.html",
        {"active_nav": "inventory", "ingest_base": str(request.base_url).rstrip("/")},
    )


@router.post("/costs/sams-paste/preview", response_class=HTMLResponse)
async def sams_paste_preview(
    request: Request,
    pasted: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Parse the pasted blob and render a review table with a best-guess SKU
    match, paid price, and pack qty per line for the operator to confirm."""
    lines = parse_sams_paste(pasted)
    products = db.query(Product).order_by(Product.name).all()
    rows = []
    for idx, line in enumerate(lines):
        match = _best_product_match(line.name, products)
        rows.append(
            {
                "idx": idx,
                "name": line.name,
                "paid_price": line.paid_price,
                "qty": line.qty,
                "match_id": match.id if match else None,
                "match_name": match.name if match else "",
            }
        )
    return templates.TemplateResponse(
        request,
        "inventory/_sams_paste_review.html",
        {"rows": rows, "products": products, "parsed_count": len(rows)},
    )


@router.post("/costs/sams-paste/save", response_class=HTMLResponse)
async def sams_paste_save(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Persist the confirmed review rows as Sam's Club ProductSource costs.

    Per row the form carries ``save_<i>`` (checkbox), ``product_<i>`` (chosen
    SKU), ``price_<i>`` (paid case price) and ``pack_<i>``. Only checked rows
    with a matched product and a positive price are written.
    """
    form = await request.form()
    idxs = {k[len("save_") :] for k in form if k.startswith("save_")}
    saved = 0
    skipped: list[str] = []
    for i in sorted(idxs, key=lambda s: _to_int(s) or 0):
        pid = _to_int(str(form.get(f"product_{i}") or ""))
        price = _to_float(str(form.get(f"price_{i}") or ""))
        label = str(form.get(f"name_{i}") or f"row {i}")
        if not pid or price is None or price <= 0:
            skipped.append(label)
            continue
        product = db.get(Product, pid)
        if not product:
            skipped.append(label)
            continue
        pack = _to_int(str(form.get(f"pack_{i}") or ""))
        _upsert_product_source_from_comparison(
            db,
            product,
            vendor_name="Sam's Club",
            vendor_key="sams_club",
            case_price=price,
            case_qty=pack,
            notes="Sam's purchase history (actual paid)",
            origin=_SAMS_PASTE_ORIGIN,
        )
        if pack and not product.case_pack_qty:
            product.case_pack_qty = pack
        saved += 1
    db.commit()
    return templates.TemplateResponse(
        request,
        "inventory/cost_import_result.html",
        {
            "active_nav": "inventory",
            "saved": saved,
            "unmatched": [],
            "skipped": skipped,
            "supplier_name": "Sam's Club",
        },
    )


# ----------------------------------------------------------------------
# Market reference (read-only competitor pricing — never a cost)
# ----------------------------------------------------------------------
# The comparator's cost side (ProductSource rows) drives margins. This is the
# *market* side: what other sellers charge for a similar item, gathered from
# free programmatic sources (UPCitemdb, Open Prices, BLS, eBay). It is display
# only — nothing here is written as a ProductSource, so it can never inflate or
# undercut a real cost.


@router.post("/compare/market", response_class=HTMLResponse)
def compare_market(
    request: Request,
    product_id: str = Form(""),
    product_query: str = Form(""),
    upc: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Gather market-reference prices for a product and render the panel.

    Persists a corrected/entered UPC back onto the product so future lookups
    skip re-typing it. The barcode keys UPCitemdb/Open Prices; the query keys
    eBay; the product category keys the BLS backdrop.
    """
    product = db.get(Product, _to_int(product_id) or 0) if product_id else None
    clean_upc = _clean_upc(upc)
    if product and clean_upc and product.upc != clean_upc:
        product.upc = clean_upc
        db.commit()

    query = product_query.strip() or (product.name if product else "")
    upc_to_use = clean_upc or (product.upc if product else "") or ""
    category = product.category if product else None
    refs = gather_market_reference(query=query, upc=upc_to_use, category=category)
    return templates.TemplateResponse(
        request,
        "inventory/_market_reference.html",
        {"refs": refs, "query": query, "upc": upc_to_use, "product": product},
    )


# Keyless UPCitemdb search is capped at ~100 lookups/day, so cap a single
# backfill run below that to avoid burning the quota in one click.
_UPC_FILL_LIMIT = 90


@router.post("/upc/fill-missing")
def fill_missing_upcs(db: Session = Depends(get_db)) -> RedirectResponse:
    """Auto-populate barcodes for active products that lack one, by resolving each
    product name through UPCitemdb. Best-effort and reviewable: only fills blanks
    (never overwrites an operator-set UPC) and is capped per run for the quota.
    A wrong guess can be corrected on the product form."""
    todo = (
        db.query(Product)
        .filter(Product.is_active.is_(True))
        .filter((Product.upc.is_(None)) | (Product.upc == ""))
        .order_by(Product.name)
        .limit(_UPC_FILL_LIMIT)
        .all()
    )
    filled = 0
    for p in todo:
        query = (f"{p.brand} {p.name}".strip() if p.brand else p.name) or ""
        code = search_upc(query)
        if code:
            p.upc = code
            filled += 1
    db.commit()
    return RedirectResponse(
        url=f"/inventory/?tab=products&upcs_filled={filled}&upcs_scanned={len(todo)}",
        status_code=303,
    )


# ----------------------------------------------------------------------
# Inventory v2: bulk + per-row background sourcing
# ----------------------------------------------------------------------
# The catalog used to require the operator to open each SKU, click into Find
# Product Prices, run a search, and save the best result — 25 clicks for an
# empty catalog. The two endpoints below collapse that to (a) one button that
# sources every unsourced SKU and (b) a per-row refresh that runs in the
# background and auto-saves the cheapest primary result. Both share
# _auto_save_best_for_product, which picks the cheapest non-AI-fallback row
# from a finished AgentJob and upserts a ProductSource with origin
# "comparator_auto".


def _auto_save_best_for_product(db: Session, job_id: int, product_id: int) -> ProductSource | None:
    """Pick the cheapest primary result from a finished price_comparison job
    and persist it as a ProductSource for ``product_id``.

    Primary = source in {api, scrape}, i.e. direct vendor extraction. AI
    fallback rows (source="ai_search") and off-domain referrals
    (vendor_key starting "cm_ref_" or "ai_ref_") are skipped — the operator
    can still save those manually from the results page if they want.
    """
    job = db.get(AgentJob, job_id)
    if not job or job.status != "done":
        return None
    product = db.get(Product, product_id)
    if not product:
        return None
    try:
        rows = json.loads(job.draft_body or "[]")
    except json.JSONDecodeError:
        return None
    priced: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("source") not in ("api", "scrape"):
            continue
        vk = r.get("vendor_key") or ""
        if vk.startswith(("cm_ref_", "ai_ref_")):
            continue
        if r.get("unit_price"):
            priced.append(r)
    if not priced:
        return None
    best = min(priced, key=lambda r: float(r["unit_price"]))
    src = _upsert_product_source_from_comparison(
        db,
        product,
        vendor_name=best.get("vendor_name") or best.get("vendor_key") or "Unknown",
        vendor_key=best.get("vendor_key", ""),
        unit_price=best.get("unit_price"),
        case_price=best.get("case_price"),
        case_qty=best.get("case_qty"),
        unit_size=best.get("unit_size"),
        url=best.get("url"),
        notes=f"Auto-saved from job #{job.id}: {best.get('product_name', '')}",
        origin="comparator_auto",
    )
    db.commit()
    return src


def _build_comparison_params(product: Product, vendor_cfg: dict) -> dict:
    """Construct the AgentJob input_params for a single-SKU price comparison."""
    return {
        "product_query": product.name,
        "vendors": COMPARATOR_FETCH_KEYS,
        "search_provider": "tavily" if app_settings.tavily_api_key else "duckduckgo",
        "vendor_config": vendor_cfg,
        "product_id": product.id,
        "fallback_case_pack": product.case_pack_qty,
    }


def _refresh_status_fragment(product_id: int, job_id: int) -> str:
    """HTML for the per-row spinner while a refresh job is in flight."""
    return (
        f'<span id="row-refresh-{product_id}" class="d-inline" '
        f'hx-get="/inventory/{product_id}/refresh-prices/status?job_id={job_id}" '
        f'hx-trigger="every 3s" hx-swap="outerHTML">'
        '<span class="spinner-border spinner-border-sm text-primary" role="status" '
        'title="Refreshing prices…"></span>'
        "</span>"
    )


def _refresh_done_fragment(product_id: int, ok: bool) -> str:
    """HTML for the per-row button after a refresh completes."""
    icon = "bi-arrow-clockwise" if ok else "bi-exclamation-circle"
    color = "btn-outline-success" if ok else "btn-outline-warning"
    return (
        f'<span id="row-refresh-{product_id}" class="d-inline" '
        f'title="{"Refresh complete — reload to see new Best Source" if ok else "Refresh hit an error — see job log"}">'
        f'<button class="btn btn-sm {color} py-0 px-1" '
        f'hx-post="/inventory/{product_id}/refresh-prices" '
        f'hx-target="#row-refresh-{product_id}" hx-swap="outerHTML" '
        f'title="Refresh prices from all vendors (auto-saves the best)">'
        f'<i class="bi {icon}"></i>'
        f"</button></span>"
    )


@router.post("/{product_id}/refresh-prices", response_class=HTMLResponse)
def refresh_prices_for_product(
    product_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Spawn a single-SKU price comparison and auto-save the best result."""
    product = _get_product_or_404(db, product_id)
    vendor_cfg = load_vendor_settings(db)
    job = AgentJob(
        job_type="price_comparison",
        status="pending",
        input_params=json.dumps(_build_comparison_params(product, vendor_cfg)),
    )
    db.add(job)
    db.commit()

    job_id = job.id
    pid = product.id

    def _run_and_save() -> None:
        price_comparator.run_price_comparison_job(job_id)
        with Session(engine) as inner:
            _auto_save_best_for_product(inner, job_id, pid)

    background_tasks.add_task(_run_and_save)
    return HTMLResponse(_refresh_status_fragment(pid, job_id))


@router.get("/{product_id}/refresh-prices/status", response_class=HTMLResponse)
def refresh_prices_status(
    product_id: int,
    job_id: int = Query(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Polled by the per-row spinner until the background refresh finishes."""
    job = db.get(AgentJob, job_id)
    if not job or job.status in ("pending", "running"):
        return HTMLResponse(_refresh_status_fragment(product_id, job_id))
    return HTMLResponse(_refresh_done_fragment(product_id, ok=(job.status == "done")))


def _bulk_source_missing(product_ids: list[int]) -> None:
    """Sequentially source missing prices for each Product ID.

    One AgentJob per SKU, run synchronously inside this coroutine. After each
    completes, the cheapest primary result is auto-saved as a ProductSource
    (origin="comparator_auto") and a progress counter is updated in
    AppSetting so the catalog's status strip can render live counts.

    The loop is idempotent against re-trigger: any SKU that has gained a
    ProductSource between queueing and execution is skipped.
    """
    sourced = 0
    for i, pid in enumerate(product_ids, start=1):
        try:
            with Session(engine) as db:
                product = db.get(Product, pid)
                if not product or product.sources:
                    _set_setting(db, "bulk_sourcing_done", str(i))
                    continue
                vendor_cfg = load_vendor_settings(db)
                job = AgentJob(
                    job_type="price_comparison",
                    status="pending",
                    input_params=json.dumps(_build_comparison_params(product, vendor_cfg)),
                )
                db.add(job)
                db.commit()
                job_id = job.id
            price_comparator.run_price_comparison_job(job_id)
            with Session(engine) as db:
                saved = _auto_save_best_for_product(db, job_id, pid)
                if saved is not None:
                    sourced += 1
                _set_setting(db, "bulk_sourcing_done", str(i))
                _set_setting(db, "bulk_sourcing_sourced", str(sourced))
        except Exception:  # noqa: BLE001 — bulk loop must not die on one SKU
            logger.exception("Bulk source: failed on product_id=%s", pid)
            with Session(engine) as db:
                _set_setting(db, "bulk_sourcing_done", str(i))
    with Session(engine) as db:
        _set_setting(db, "bulk_sourcing_in_progress", "")


@router.post("/bulk-source/run")
def source_missing_run(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Queue background price comparisons for every unsourced SKU."""
    if _get_setting(db, "bulk_sourcing_in_progress", "") == "1":
        return RedirectResponse(url="/inventory/?tab=products", status_code=303)

    unsourced_ids: list[int] = []
    for p in (
        db.query(Product)
        .options(selectinload(Product.sources))
        .filter(Product.is_active.is_(True))
        .all()
    ):
        if not p.sources:
            unsourced_ids.append(p.id)

    if not unsourced_ids:
        return RedirectResponse(url="/inventory/?tab=products", status_code=303)

    _set_setting(db, "bulk_sourcing_total", str(len(unsourced_ids)))
    _set_setting(db, "bulk_sourcing_done", "0")
    _set_setting(db, "bulk_sourcing_sourced", "0")
    _set_setting(db, "bulk_sourcing_in_progress", "1")
    background_tasks.add_task(_bulk_source_missing, unsourced_ids)
    return RedirectResponse(url="/inventory/?tab=products", status_code=303)


@router.get("/bulk-source/status", response_class=HTMLResponse)
def source_missing_status(db: Session = Depends(get_db)) -> HTMLResponse:
    """Live progress strip for the bulk-source run.

    While running: a progress bar with "X of Y done · Z priced". When a run has
    just finished: a one-line summary (shown once, then consumed so a later page
    load is clean). Otherwise an empty div.
    """
    active = _get_setting(db, "bulk_sourcing_in_progress", "") == "1"

    def _int(key: str) -> int:
        try:
            return int(_get_setting(db, key, "0") or 0)
        except ValueError:
            return 0

    done = _int("bulk_sourcing_done")
    total = _int("bulk_sourcing_total")
    sourced = _int("bulk_sourcing_sourced")

    if not active:
        # A finished run leaves total>0 until its summary is shown once here.
        if total > 0:
            _set_setting(db, "bulk_sourcing_total", "0")  # consume — clean on reload
            empty = total - sourced
            tail = (
                '<a href="/inventory/?tab=products#bulkCosts" '
                'class="alert-link" data-bs-toggle="collapse" data-bs-target="#bulkCosts">'
                "enter those costs manually</a>"
                if empty > 0
                else "all set"
            )
            note = (
                f' <span class="text-muted">{empty} came back empty — {tail}.</span>'
                if empty > 0
                else ""
            )
            return HTMLResponse(
                f'<div id="bulk-source-strip" '
                f'class="alert alert-success py-2 small mb-3 d-flex align-items-center gap-2">'
                f'<i class="bi bi-check2-circle"></i>'
                f"<span><strong>Sourcing complete.</strong> "
                f"Saved a price for {sourced} of {total} SKUs.{note}</span>"
                f'<button type="button" class="btn-close ms-auto" style="font-size:.6rem" '
                f'data-bs-dismiss="alert" aria-label="Dismiss"></button>'
                f"</div>"
            )
        return HTMLResponse('<div id="bulk-source-strip"></div>')

    pct = (done / total * 100) if total else 0
    return HTMLResponse(
        f'<div id="bulk-source-strip" '
        f'hx-get="/inventory/bulk-source/status" '
        f'hx-trigger="every 3s" hx-swap="outerHTML" '
        f'class="alert alert-info py-2 small mb-3 d-flex align-items-center gap-2">'
        f'<span class="spinner-border spinner-border-sm text-primary"></span>'
        f"<strong>Sourcing missing prices…</strong>"
        f'<span class="text-muted">{done} of {total} SKUs done · {sourced} priced</span>'
        f'<div class="progress flex-grow-1" style="height:6px">'
        f'<div class="progress-bar" role="progressbar" style="width: {pct:.0f}%"></div>'
        f"</div></div>"
    )
