from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)
from sqlalchemy import func as sql_func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import AgentJob
from app.models.sales import OutreachLog, Prospect
from app.services import agent, email_sender
from app.views import templates

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


@router.get("/", response_class=HTMLResponse)
def leads_index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    jobs = (
        db.query(AgentJob)
        .filter(AgentJob.job_type.in_(["research", "email_draft"]))
        .order_by(AgentJob.created_at.desc())
        .limit(100)
        .all()
    )
    current_provider = _get_setting(db, "search_provider", "duckduckgo")
    return templates.TemplateResponse(
        request,
        "leads/index.html",
        {
            "active_nav": "leads",
            "jobs": jobs,
            "prospect_names": _prospect_names(db, jobs),
            "venue_options": VENUE_TYPE_OPTIONS,
            "current_provider": current_provider,
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
        raise HTTPException(status_code=500, detail="Database error creating job")
    background_tasks.add_task(agent.run_research_job, job.id)
    return RedirectResponse(url=f"/leads/jobs/{job.id}", status_code=303)


@router.get("/jobs/", response_class=HTMLResponse)
def leads_jobs_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    jobs = (
        db.query(AgentJob)
        .filter(AgentJob.job_type.in_(["research", "email_draft"]))
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
    return RedirectResponse(url=f"/leads/jobs/{job.id}", status_code=303)


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
