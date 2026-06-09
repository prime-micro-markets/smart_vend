from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func as sql_func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.agent import AgentJob
from app.models.sales import OutreachLog, Prospect
from app.models.sam_contract import SavedContract
from app.models.scout import ScoutedLocation
from app.services import agent, email_sender, geo_scout, sam_gov
from app.views import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/leads", tags=["leads"])

VENUE_TYPE_OPTIONS = [
    "gym",
    "hotel",
    "corporate office",
    "condo / apartment complex",
    "hospital / medical",
    "university / college",
    "government / military",
    "retail / shopping",
]


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


def _prospect_names(db: Session, jobs: list[AgentJob]) -> dict[int, str]:
    ids = [j.prospect_id for j in jobs if j.job_type == "email_draft" and j.prospect_id]
    if not ids:
        return {}
    rows = db.query(Prospect.id, Prospect.company_name).filter(Prospect.id.in_(ids)).all()
    return {r[0]: r[1] for r in rows}


def _outreach_status(
    db: Session, prospects: list[Prospect], email_jobs: list[AgentJob]
) -> dict[int, dict]:
    """Per-prospect outreach state for the Email tab badges: whether an email was
    actually sent (``OutreachLog(outcome="sent")``) and whether a draft is ready."""
    status: dict[int, dict] = {}
    # Latest ready email_draft job per prospect (jobs already ordered newest-first).
    for job in email_jobs:
        if not job.prospect_id or job.prospect_id in status:
            continue
        if job.status == "done" and job.draft_body:
            status[job.prospect_id] = {"has_draft": True, "last_draft_job_id": job.id}
    for p in prospects:
        entry = status.setdefault(p.id, {"has_draft": False, "last_draft_job_id": None})
        sent = [log for log in p.outreach_logs if log.outcome == "sent"]
        entry["sent_at"] = sent[0].contacted_at if sent else None
    return status


@router.get("/", response_class=HTMLResponse)
def leads_index(
    request: Request, tab: str = "research", db: Session = Depends(get_db)
) -> HTMLResponse:
    jobs = (
        db.query(AgentJob)
        .filter(AgentJob.job_type == "research")
        .order_by(AgentJob.created_at.desc())
        .limit(100)
        .all()
    )
    email_jobs = (
        db.query(AgentJob)
        .filter(AgentJob.job_type == "email_draft")
        .order_by(AgentJob.created_at.desc())
        .limit(100)
        .all()
    )
    prospects = db.query(Prospect).order_by(Prospect.updated_at.desc()).all()
    current_provider = _get_setting(db, "search_provider", "duckduckgo")
    saved_contracts = db.query(SavedContract).order_by(SavedContract.saved_at.desc()).all()
    scouted = db.query(ScoutedLocation).order_by(ScoutedLocation.opportunity_score.desc()).all()
    return templates.TemplateResponse(
        request,
        "leads/index.html",
        {
            "active_nav": "leads",
            "active_tab": tab if tab in ("research", "contracts", "scout", "email") else "research",
            "jobs": jobs,
            # Email Outreach tab
            "email_jobs": email_jobs,
            "prospects": prospects,
            "prospect_names": _prospect_names(db, email_jobs),
            "outreach_status": _outreach_status(db, prospects, email_jobs),
            "venue_options": VENUE_TYPE_OPTIONS,
            "current_provider": current_provider,
            # Gov Contracts tab
            "sam_configured": bool(settings.sam_gov_api_key),
            "saved_contracts": saved_contracts,
            "saved_notice_ids": {c.notice_id for c in saved_contracts},
            "veteran_set_asides": sam_gov.VETERAN_SET_ASIDES,
            "other_set_asides": sam_gov.OTHER_SET_ASIDES,
            "notice_types": sam_gov.NOTICE_TYPES,
            "default_naics": sam_gov.DEFAULT_NAICS,
            # Scout Map tab
            "scout_venues": geo_scout.SCOUT_VENUE_LABELS,
            "scout_center": geo_scout.DEFAULT_CENTER,
            "scout_radius_mi": geo_scout.DEFAULT_RADIUS_MI,
            "places_configured": bool(settings.google_places_api_key),
            "scouted": scouted,
            "scouted_osm_ids": {s.osm_id for s in scouted},
        },
    )


@router.post("/research", response_class=HTMLResponse)
def leads_research(
    request: Request,
    background_tasks: BackgroundTasks,
    venue_types: list[str] = Form(default=[]),
    location_city: str = Form("Panama City"),
    location_state: str = Form("FL"),
    search_focus: str = Form(""),
    max_leads: int = Form(20),
    preview_mode: bool = Form(False),
    search_provider: str = Form("duckduckgo"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    max_leads = max(1, min(50, max_leads))

    # Persist provider choice as new default
    _set_setting(db, "search_provider", search_provider)

    # Auto-reset stale research jobs stuck for over 2 hours
    stale_cutoff = datetime.now() - timedelta(hours=2)
    db.query(AgentJob).filter(
        AgentJob.job_type == "research",
        AgentJob.status.in_(["running", "pending"]),
        AgentJob.created_at < stale_cutoff,
    ).update({"status": "error", "error_message": "Auto-reset: exceeded 2-hour limit"})
    db.commit()

    location = f"{location_city.strip()}, {location_state.strip()}"
    params = {
        "venue_types": venue_types,
        "location": location,
        "search_focus": search_focus.strip(),
        "max_leads": max_leads,
        "search_provider": search_provider,
    }
    job = AgentJob(
        job_type="research",
        status="pending",
        input_params=json.dumps(params),
        preview_mode=preview_mode,
    )
    try:
        db.add(job)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to create research job")
        raise HTTPException(status_code=500, detail="Database error creating job") from None
    background_tasks.add_task(agent.run_research_job, job.id)
    return RedirectResponse(url=f"/leads/jobs/{job.id}", status_code=303)


@router.get("/jobs/", response_class=HTMLResponse)
def leads_jobs_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    jobs = (
        db.query(AgentJob)
        .filter(AgentJob.job_type == "research")
        .order_by(AgentJob.created_at.desc())
        .limit(100)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "leads/_job_history.html",
        {"jobs": jobs, "prospect_names": _prospect_names(db, jobs)},
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def leads_job_status(job_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.get(AgentJob, job_id)
    if not job:
        return Response(status_code=404)
    prospects_list: list[Prospect] = []
    if job.status == "done" and job.job_type == "research":
        prospects_list = db.query(Prospect).filter(Prospect.source_job_id == job.id).all()
    return templates.TemplateResponse(
        request,
        "leads/job_status.html",
        {"active_nav": "leads", "job": job, "prospects_list": prospects_list},
    )


@router.get("/jobs/{job_id}/poll", response_class=HTMLResponse)
def leads_job_poll(job_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.get(AgentJob, job_id)
    if not job:
        return HTMLResponse(content="<div>Job not found</div>", status_code=404)
    prospects_list: list[Prospect] = []
    if job.status == "done" and job.job_type == "research":
        prospects_list = db.query(Prospect).filter(Prospect.source_job_id == job.id).all()
    return templates.TemplateResponse(
        request,
        "leads/_job_status_card.html",
        {"job": job, "prospects_list": prospects_list},
    )


@router.get("/jobs/{job_id}/draft", response_class=HTMLResponse)
def leads_draft_review(
    job_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    job = db.get(AgentJob, job_id)
    if not job:
        return Response(status_code=404)
    prospect = db.get(Prospect, job.prospect_id) if job.prospect_id else None
    return templates.TemplateResponse(
        request,
        "leads/draft_review.html",
        {"active_nav": "leads", "job": job, "prospect": prospect},
    )


@router.post("/prospects/{prospect_id}/draft", response_class=HTMLResponse)
def leads_draft_email(
    prospect_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    preview_mode: bool = Form(False),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    prospect = db.get(Prospect, prospect_id)
    if not prospect:
        return Response(status_code=404)
    job = AgentJob(
        job_type="email_draft",
        status="pending",
        prospect_id=prospect_id,
        preview_mode=preview_mode,
    )
    db.add(job)
    db.commit()
    background_tasks.add_task(agent.run_email_draft_job, job.id)
    # HTMX callers (Email tab, contact card) get the inline polling card swapped in
    # place; legacy plain-form posts still redirect to the full job status page.
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "leads/_job_status_card.html",
            {"job": job, "prospects_list": []},
        )
    return RedirectResponse(url=f"/leads/jobs/{job.id}", status_code=303)


@router.post("/jobs/{job_id}/rewrite", response_class=HTMLResponse)
def leads_rewrite_email(
    job_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    feedback: str = Form(...),
    subject: str = Form(""),
    body: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Re-draft an existing email with operator feedback. Reuses the previous draft
    (with any manual edits passed back as ``subject``/``body``) as the starting point
    and queues a fresh ``email_draft`` job carrying the feedback in ``input_params``."""
    prev = db.get(AgentJob, job_id)
    if not prev:
        return Response(status_code=404)
    new_job = AgentJob(
        job_type="email_draft",
        status="pending",
        prospect_id=prev.prospect_id,
        preview_mode=prev.preview_mode,
        input_params=json.dumps(
            {
                "feedback": feedback,
                "prev_subject": subject or (prev.draft_subject or ""),
                "prev_body": body or (prev.draft_body or ""),
            }
        ),
    )
    db.add(new_job)
    db.commit()
    background_tasks.add_task(agent.run_email_draft_job, new_job.id)
    # Land on the job-status page, which polls until the rewrite is ready and then
    # links to "Review & Send" for the new draft (operator can refine again there).
    return RedirectResponse(url=f"/leads/jobs/{new_job.id}", status_code=303)


@router.post("/prospects/{prospect_id}/send-direct", response_class=HTMLResponse)
def leads_send_direct(
    prospect_id: int,
    request: Request,
    subject: str = Form(...),
    body: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    prospect = db.get(Prospect, prospect_id)
    if not prospect:
        return Response(status_code=404)
    if not prospect.contact_email:
        return templates.TemplateResponse(
            request,
            "leads/job_status.html",
            {
                "active_nav": "leads",
                "job": None,
                "prospects_list": [],
                "send_error": f"No email address on file for {prospect.company_name}.",
            },
            status_code=400,
        )
    try:
        email_sender.send_email(to_address=prospect.contact_email, subject=subject, body=body)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "leads/job_status.html",
            {
                "active_nav": "leads",
                "job": None,
                "prospects_list": [],
                "send_error": str(exc),
            },
            status_code=500,
        )
    log = OutreachLog(
        prospect_id=prospect_id,
        channel="email",
        direction="outbound",
        contacted_at=datetime.now(),
        subject_or_summary=subject,
        outcome="sent",
    )
    db.add(log)
    # A real send moves a fresh lead into the "contacted" stage automatically and
    # records the first-contact date for the pipeline card / date filter.
    if prospect.pipeline_stage == "lead":
        prospect.pipeline_stage = "contacted"
    if prospect.contacted_at is None:
        prospect.contacted_at = datetime.now()
    db.commit()
    return RedirectResponse(url=f"/sales/{prospect_id}", status_code=303)


@router.post("/jobs/{job_id}/send", response_class=HTMLResponse)
def leads_send_email(
    job_id: int,
    request: Request,
    to_email: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    job = db.get(AgentJob, job_id)
    if not job:
        return Response(status_code=404)
    prospect = db.get(Prospect, job.prospect_id) if job.prospect_id else None

    if job.preview_mode:
        if prospect:
            log = OutreachLog(
                prospect_id=prospect.id,
                channel="email",
                direction="outbound",
                contacted_at=datetime.now(),
                subject_or_summary=subject,
                outcome="preview",
                notes="Preview mode — email was NOT sent.",
            )
            db.add(log)
            db.commit()
            return RedirectResponse(url=f"/sales/{prospect.id}?preview_sent=1", status_code=303)
        return RedirectResponse(url="/leads/?preview_sent=1", status_code=303)

    try:
        email_sender.send_email(to_address=to_email, subject=subject, body=body)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "leads/draft_review.html",
            {
                "active_nav": "leads",
                "job": job,
                "prospect": prospect,
                "send_error": str(exc),
            },
            status_code=500,
        )

    if prospect:
        log = OutreachLog(
            prospect_id=prospect.id,
            channel="email",
            direction="outbound",
            contacted_at=datetime.now(),
            subject_or_summary=subject,
            outcome="sent",
        )
        db.add(log)
        # A real send moves a fresh lead into the "contacted" stage automatically and
        # records the first-contact date for the pipeline card / date filter.
        if prospect.pipeline_stage == "lead":
            prospect.pipeline_stage = "contacted"
        if prospect.contacted_at is None:
            prospect.contacted_at = datetime.now()
        db.commit()
        return RedirectResponse(url=f"/sales/{prospect.id}", status_code=303)

    return RedirectResponse(url="/leads/", status_code=303)


@router.get("/usage", response_class=HTMLResponse)
def leads_usage(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    today_start = datetime.combine(date.today(), datetime.min.time())
    today_tokens = (
        db.query(sql_func.sum(AgentJob.tokens_used))
        .filter(AgentJob.created_at >= today_start)
        .scalar()
        or 0
    )
    total_tokens = db.query(sql_func.sum(AgentJob.tokens_used)).scalar() or 0
    latest = (
        db.query(AgentJob)
        .filter(AgentJob.ratelimit_tokens_remaining.isnot(None))
        .order_by(AgentJob.finished_at.desc())
        .first()
    )
    return templates.TemplateResponse(
        request,
        "leads/_usage_widget.html",
        {
            "today_tokens": today_tokens,
            "total_tokens": total_tokens,
            "latest_job": latest,
        },
    )


@router.delete("/jobs/{job_id}", response_class=HTMLResponse)
def leads_job_delete(job_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.get(AgentJob, job_id)
    if job and job.job_type in ("research", "email_draft"):
        db.delete(job)
        db.commit()
    return HTMLResponse(content="", status_code=200)


# ── Gov Contracts (SAM.gov) ──────────────────────────────────────────────────
# Live federal contract-opportunity search + saved favorites. The /contracts/*
# routes always return HTMX partials. They're declared after the catch-all-free
# job routes; none collide with the {job_id} paths.


@router.post("/contracts/search", response_class=HTMLResponse)
def leads_contracts_search(
    request: Request,
    keyword: str = Form(""),
    state: str = Form(""),
    zip_code: str = Form(""),
    naics: str = Form(""),
    set_aside: str = Form(""),
    notice_type: str = Form(""),
    posted_from: str = Form(""),
    posted_to: str = Form(""),
    offset: int = Form(0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    items, total, reason = sam_gov.search_opportunities(
        keyword=keyword,
        state=state,
        zip_code=zip_code,
        naics=naics,
        set_aside=set_aside,
        notice_type=notice_type,
        posted_from=posted_from,
        posted_to=posted_to,
        limit=25,
        offset=offset,
    )
    saved_notice_ids = {row[0] for row in db.query(SavedContract.notice_id).all()}
    return templates.TemplateResponse(
        request,
        "leads/_contract_results.html",
        {
            "items": items,
            "total": total,
            "reason": reason,
            "offset": offset,
            "saved_notice_ids": saved_notice_ids,
        },
    )


@router.post("/contracts/save", response_class=HTMLResponse)
def leads_contracts_save(
    request: Request,
    notice_id: str = Form(...),
    title: str = Form(...),
    agency: str = Form(""),
    notice_type: str = Form(""),
    posted_date: str = Form(""),
    response_deadline: str = Form(""),
    set_aside: str = Form(""),
    naics_code: str = Form(""),
    place: str = Form(""),
    solicitation_number: str = Form(""),
    ui_link: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Idempotently upsert a saved favorite, keyed by notice_id. Returns the
    'saved' button state to swap in place of the Save button."""
    existing = db.query(SavedContract).filter_by(notice_id=notice_id).first()
    if not existing:
        db.add(
            SavedContract(
                notice_id=notice_id,
                title=title,
                agency=agency or None,
                notice_type=notice_type or None,
                posted_date=posted_date or None,
                response_deadline=response_deadline or None,
                set_aside=set_aside or None,
                naics_code=naics_code or None,
                place=place or None,
                solicitation_number=solicitation_number or None,
                ui_link=ui_link or None,
            )
        )
        db.commit()
    return HTMLResponse(
        content=(
            '<span class="badge bg-success-subtle text-success border border-success-subtle">'
            '<i class="bi bi-check-lg me-1"></i>Saved</span>'
        ),
        status_code=200,
    )


@router.get("/contracts/saved/", response_class=HTMLResponse)
def leads_contracts_saved(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    saved_contracts = db.query(SavedContract).order_by(SavedContract.saved_at.desc()).all()
    return templates.TemplateResponse(
        request,
        "leads/_saved_contracts.html",
        {"saved_contracts": saved_contracts},
    )


@router.delete("/contracts/saved/{contract_id}", response_class=HTMLResponse)
def leads_contracts_saved_delete(contract_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    row = db.get(SavedContract, contract_id)
    if row:
        db.delete(row)
        db.commit()
    return HTMLResponse(content="", status_code=200)


# ── Scout Map (OpenStreetMap placement prospecting) ──────────────────────────
# Map-based business discovery + opportunity scoring. All /scout/* routes return
# HTMX partials. Free by default (Overpass/Nominatim); Google Places enriches
# drill-down when configured. See app/services/geo_scout.py.


@router.post("/scout/search", response_class=HTMLResponse)
def leads_scout_search(
    request: Request,
    query: str = Form(""),
    lat: float = Form(geo_scout.DEFAULT_CENTER[0]),
    lng: float = Form(geo_scout.DEFAULT_CENTER[1]),
    radius_mi: float = Form(geo_scout.DEFAULT_RADIUS_MI),
    venue_filter: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    center_label = ""
    if query.strip():
        geo = geo_scout.geocode(query)
        if geo:
            lat, lng, center_label = geo
    items, reason = geo_scout.scout(
        lat=lat, lng=lng, radius_mi=radius_mi, venue_labels=venue_filter or None
    )
    scouted_osm_ids = {row[0] for row in db.query(ScoutedLocation.osm_id).all()}
    markers = [
        {
            "osm_id": b.osm_id,
            "name": b.name,
            "lat": b.lat,
            "lng": b.lng,
            "score": b.opportunity_score,
            "venue_type": b.venue_type,
        }
        for b in items
    ]
    return templates.TemplateResponse(
        request,
        "leads/_scout_results.html",
        {
            "items": items,
            "reason": reason,
            "center_lat": lat,
            "center_lng": lng,
            "center_label": center_label,
            "radius_mi": radius_mi,
            "markers_json": json.dumps(markers),
            "scouted_osm_ids": scouted_osm_ids,
        },
    )


@router.post("/scout/biz", response_class=HTMLResponse)
def leads_scout_biz(
    request: Request,
    osm_id: str = Form(...),
    name: str = Form(...),
    venue_type: str = Form(""),
    lat: float = Form(...),
    lng: float = Form(...),
    address: str = Form(""),
    phone: str = Form(""),
    website: str = Form(""),
    has_vending_guess: str = Form("unknown"),
    nearest_food_mi: str = Form(""),
    opportunity_score: int = Form(0),
    fit_tags: str = Form("[]"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Drill-down detail panel for one business. Enriches contact data via Google
    Places when a key is configured; otherwise shows the OSM record."""
    extra = geo_scout.enrich_place(name, lat, lng)
    try:
        tags = json.loads(fit_tags) if fit_tags else []
    except ValueError:
        tags = []
    biz = {
        "osm_id": osm_id,
        "name": name,
        "venue_type": venue_type,
        "lat": lat,
        "lng": lng,
        "address": address,
        "phone": extra.get("phone") or phone,
        "website": extra.get("website") or website,
        "has_vending_guess": has_vending_guess,
        "nearest_food_mi": nearest_food_mi,
        "opportunity_score": opportunity_score,
        "fit_tags": tags,
        "hours": extra.get("hours"),
        "rating": extra.get("rating"),
        "enriched": bool(extra),
    }
    scout_row = db.query(ScoutedLocation).filter_by(osm_id=osm_id).first()
    return templates.TemplateResponse(
        request,
        "leads/_scout_detail.html",
        {
            "biz": biz,
            "is_saved": scout_row is not None,
            "scout": scout_row,
            "places_configured": bool(settings.google_places_api_key),
        },
    )


def _get_or_create_scouted(
    db: Session,
    *,
    osm_id: str,
    name: str,
    venue_type: str,
    lat: float,
    lng: float,
    address: str,
    phone: str,
    website: str,
    has_vending_guess: str,
    nearest_food_mi: str,
    opportunity_score: int,
    fit_tags: str,
) -> ScoutedLocation:
    """Idempotent upsert of a scouted business keyed by osm_id (shared by the
    Save and AI-enrich routes)."""
    existing = db.query(ScoutedLocation).filter_by(osm_id=osm_id).first()
    if existing:
        return existing
    try:
        food_mi = float(nearest_food_mi) if nearest_food_mi not in ("", "None") else None
    except ValueError:
        food_mi = None
    row = ScoutedLocation(
        osm_id=osm_id,
        name=name,
        venue_type=venue_type or None,
        address=address or None,
        lat=lat,
        lng=lng,
        phone=phone or None,
        website=website or None,
        opportunity_score=opportunity_score,
        nearest_food_mi=food_mi,
        has_vending_guess=has_vending_guess or "unknown",
        signals=fit_tags or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.post("/scout/save", response_class=HTMLResponse)
def leads_scout_save(
    request: Request,
    osm_id: str = Form(...),
    name: str = Form(...),
    venue_type: str = Form(""),
    lat: float = Form(...),
    lng: float = Form(...),
    address: str = Form(""),
    phone: str = Form(""),
    website: str = Form(""),
    has_vending_guess: str = Form("unknown"),
    nearest_food_mi: str = Form(""),
    opportunity_score: int = Form(0),
    fit_tags: str = Form("[]"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Idempotently upsert a scouted business, keyed by osm_id. Returns a 'Saved'
    badge to swap in place of the Save button."""
    _get_or_create_scouted(
        db,
        osm_id=osm_id,
        name=name,
        venue_type=venue_type,
        lat=lat,
        lng=lng,
        address=address,
        phone=phone,
        website=website,
        has_vending_guess=has_vending_guess,
        nearest_food_mi=nearest_food_mi,
        opportunity_score=opportunity_score,
        fit_tags=fit_tags,
    )
    return HTMLResponse(
        content=(
            '<span class="badge bg-success-subtle text-success border border-success-subtle">'
            '<i class="bi bi-check-lg me-1"></i>Saved</span>'
        ),
        status_code=200,
    )


@router.post("/scout/enrich", response_class=HTMLResponse)
def leads_scout_enrich(
    request: Request,
    background_tasks: BackgroundTasks,
    osm_id: str = Form(...),
    name: str = Form(...),
    venue_type: str = Form(""),
    lat: float = Form(...),
    lng: float = Form(...),
    address: str = Form(""),
    phone: str = Form(""),
    website: str = Form(""),
    has_vending_guess: str = Form("unknown"),
    nearest_food_mi: str = Form(""),
    opportunity_score: int = Form(0),
    fit_tags: str = Form("[]"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Save (if needed) and kick off the AI deep-dive for one business. Returns
    the polling panel that streams status until the job finishes."""
    row = _get_or_create_scouted(
        db,
        osm_id=osm_id,
        name=name,
        venue_type=venue_type,
        lat=lat,
        lng=lng,
        address=address,
        phone=phone,
        website=website,
        has_vending_guess=has_vending_guess,
        nearest_food_mi=nearest_food_mi,
        opportunity_score=opportunity_score,
        fit_tags=fit_tags,
    )
    # Don't start a second job if one is already running for this row.
    if row.ai_status not in ("pending", "running"):
        job = AgentJob(
            job_type="scout_enrich",
            status="pending",
            input_params=json.dumps({"scout_id": row.id}),
        )
        db.add(job)
        db.commit()
        row.ai_status = "pending"
        row.ai_job_id = job.id
        db.commit()
        background_tasks.add_task(agent.run_scout_enrich_job, job.id)
    return templates.TemplateResponse(
        request,
        "leads/_scout_enrich.html",
        {"scout": row},
    )


@router.get("/scout/enrich/{scout_id}/poll", response_class=HTMLResponse)
def leads_scout_enrich_poll(
    scout_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    row = db.get(ScoutedLocation, scout_id)
    if not row:
        return HTMLResponse(content="<div>Not found</div>", status_code=404)
    return templates.TemplateResponse(
        request,
        "leads/_scout_enrich.html",
        {"scout": row},
    )


@router.get("/scout/saved/", response_class=HTMLResponse)
def leads_scout_saved(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    scouted = db.query(ScoutedLocation).order_by(ScoutedLocation.opportunity_score.desc()).all()
    return templates.TemplateResponse(
        request,
        "leads/_scout_saved.html",
        {"scouted": scouted},
    )


@router.post("/scout/{scout_id}/promote", response_class=HTMLResponse)
def leads_scout_promote(scout_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """Promote a scouted business into the sales pipeline as a Prospect."""
    row = db.get(ScoutedLocation, scout_id)
    if not row:
        return Response(status_code=404)
    if row.promoted_prospect_id:
        return RedirectResponse(url=f"/sales/{row.promoted_prospect_id}", status_code=303)
    note_bits = [f"Opportunity score {row.opportunity_score}/100 (Scout Map)."]
    if row.nearest_food_mi is not None:
        note_bits.append(f"Nearest food/store: {row.nearest_food_mi} mi.")
    if row.has_vending_guess and row.has_vending_guess != "unknown":
        note_bits.append(f"Vending (map): {row.has_vending_guess}.")
    # Fold in the AI deep-dive findings when present.
    if row.ai_summary:
        note_bits.append(f"AI: {row.ai_summary}")
    if row.ai_employees:
        note_bits.append(f"Est. employees: {row.ai_employees}.")
    if row.ai_has_vending:
        note_bits.append(f"Vending (AI): {row.ai_has_vending}.")
    prospect = Prospect(
        company_name=row.name,
        venue_type=row.venue_type,
        address=row.address,
        website=row.website,
        contact_name=row.ai_contact_name,
        contact_title=row.ai_contact_title,
        contact_email=row.ai_contact_email,
        contact_phone=row.ai_contact_phone or row.phone,
        foot_traffic_estimate=row.ai_foot_traffic,
        tier=geo_scout.score_to_tier(row.opportunity_score),
        source="scout_map",
        notes=" ".join(note_bits),
    )
    db.add(prospect)
    db.flush()
    row.status = "promoted"
    row.promoted_prospect_id = prospect.id
    db.commit()
    return RedirectResponse(url=f"/sales/{prospect.id}", status_code=303)


@router.delete("/scout/saved/{scout_id}", response_class=HTMLResponse)
def leads_scout_saved_delete(scout_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    row = db.get(ScoutedLocation, scout_id)
    if row:
        db.delete(row)
        db.commit()
    return HTMLResponse(content="", status_code=200)
