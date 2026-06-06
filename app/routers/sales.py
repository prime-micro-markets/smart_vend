import json
from datetime import date, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import AgentJob
from app.models.location import Location
from app.models.sales import OutreachLog, Prospect
from app.services import agent
from app.views import templates

router = APIRouter(prefix="/sales", tags=["sales"])

PIPELINE_STAGES = ["lead", "contacted", "site_visit", "proposal", "signed", "lost"]


@router.get("/", response_class=HTMLResponse)
def sales_index(
    request: Request,
    location_id: int | None = None,
    tab: str = "board",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = db.query(Prospect).order_by(Prospect.next_action_date)
    if location_id:
        query = query.filter(Prospect.location_id == location_id)
    prospects_by_stage: dict[str, list[Prospect]] = {s: [] for s in PIPELINE_STAGES}
    for prospect in query.all():
        stage = prospect.pipeline_stage if prospect.pipeline_stage in prospects_by_stage else "lead"
        prospects_by_stage[stage].append(prospect)
    locations = db.query(Location).order_by(Location.name).all()
    active_location = db.get(Location, location_id) if location_id else None
    # Locations tab data: pipeline leads not yet signed/lost (mirrors the old
    # standalone Locations page that this page now absorbs as a second tab).
    pipeline_leads = (
        db.query(Prospect)
        .filter(Prospect.pipeline_stage.notin_(["signed", "lost"]))
        .order_by(Prospect.company_name)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "sales/index.html",
        {
            "active_nav": "sales",
            "prospects_by_stage": prospects_by_stage,
            "stages": PIPELINE_STAGES,
            "locations": locations,
            "active_location": active_location,
            "pipeline_leads": pipeline_leads,
            "active_tab": "locations" if tab == "locations" else "board",
        },
    )


@router.get("/new", response_class=HTMLResponse)
def prospect_new_form(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    locations = db.query(Location).order_by(Location.name).all()
    return templates.TemplateResponse(
        request, "sales/_prospect_form.html", {"prospect": None, "locations": locations}
    )


@router.get("/{prospect_id}/edit", response_class=HTMLResponse)
def prospect_edit_form(
    prospect_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    prospect = db.get(Prospect, prospect_id)
    if not prospect:
        return Response(status_code=404)
    locations = db.query(Location).order_by(Location.name).all()
    return templates.TemplateResponse(
        request, "sales/_prospect_form.html", {"prospect": prospect, "locations": locations}
    )


@router.get("/{prospect_id}/card", response_class=HTMLResponse)
def prospect_card_modal(
    prospect_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    prospect = db.get(Prospect, prospect_id)
    if not prospect:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request,
        "sales/_prospect_modal.html",
        {"prospect": prospect},
    )


@router.post("/{prospect_id}/enrich", response_class=HTMLResponse)
def prospect_enrich(
    prospect_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Kick off the AI deep-dive for one prospect (same engine as the Scout Map).
    Returns the polling panel that streams status until the job finishes."""
    prospect = db.get(Prospect, prospect_id)
    if not prospect:
        return Response(status_code=404)
    # Don't start a second job if one is already running for this prospect.
    if prospect.ai_status not in ("pending", "running"):
        job = AgentJob(
            job_type="prospect_enrich",
            status="pending",
            input_params=json.dumps({"prospect_id": prospect.id}),
        )
        db.add(job)
        db.commit()
        prospect.ai_status = "pending"
        prospect.ai_job_id = job.id
        db.commit()
        background_tasks.add_task(agent.run_prospect_enrich_job, job.id)
    return templates.TemplateResponse(
        request, "sales/_prospect_enrich.html", {"prospect": prospect}
    )


@router.get("/{prospect_id}/enrich/poll", response_class=HTMLResponse)
def prospect_enrich_poll(
    prospect_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    prospect = db.get(Prospect, prospect_id)
    if not prospect:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request, "sales/_prospect_enrich.html", {"prospect": prospect}
    )


@router.post("/", response_class=HTMLResponse)
def prospect_create(
    request: Request,
    company_name: str = Form(...),
    contact_name: str = Form(""),
    contact_title: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    linkedin_url: str = Form(""),
    website: str = Form(""),
    venue_type: str = Form(""),
    address: str = Form(""),
    city: str = Form("Panama City"),
    state: str = Form(""),
    tier: str = Form(""),
    source: str = Form(""),
    estimated_machines: int = Form(1),
    foot_traffic_estimate: str = Form(""),
    next_action: str = Form(""),
    next_action_date: str = Form(""),
    notes: str = Form(""),
    location_id: int = Form(0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    prospect = Prospect(
        company_name=company_name,
        contact_name=contact_name or None,
        contact_title=contact_title or None,
        contact_email=contact_email or None,
        contact_phone=contact_phone or None,
        linkedin_url=linkedin_url or None,
        website=website or None,
        venue_type=venue_type or None,
        address=address or None,
        city=city,
        state=state or None,
        tier=tier or None,
        source=source or None,
        estimated_machines=estimated_machines,
        foot_traffic_estimate=foot_traffic_estimate or None,
        next_action=next_action or None,
        next_action_date=date.fromisoformat(next_action_date) if next_action_date else None,
        notes=notes or None,
        location_id=location_id or None,
    )
    db.add(prospect)
    db.commit()
    return RedirectResponse(url="/sales/", status_code=303)


@router.post("/{prospect_id}", response_class=HTMLResponse)
def prospect_update(
    prospect_id: int,
    company_name: str = Form(...),
    contact_name: str = Form(""),
    contact_title: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    linkedin_url: str = Form(""),
    website: str = Form(""),
    venue_type: str = Form(""),
    address: str = Form(""),
    city: str = Form("Panama City"),
    state: str = Form(""),
    tier: str = Form(""),
    source: str = Form(""),
    estimated_machines: int = Form(1),
    foot_traffic_estimate: str = Form(""),
    next_action: str = Form(""),
    next_action_date: str = Form(""),
    notes: str = Form(""),
    location_id: int = Form(0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    prospect = db.get(Prospect, prospect_id)
    if not prospect:
        return Response(status_code=404)
    prospect.company_name = company_name
    prospect.contact_name = contact_name or None
    prospect.contact_title = contact_title or None
    prospect.contact_email = contact_email or None
    prospect.contact_phone = contact_phone or None
    prospect.linkedin_url = linkedin_url or None
    prospect.website = website or None
    prospect.venue_type = venue_type or None
    prospect.address = address or None
    prospect.city = city
    prospect.state = state or None
    prospect.tier = tier or None
    prospect.source = source or None
    prospect.estimated_machines = estimated_machines
    prospect.foot_traffic_estimate = foot_traffic_estimate or None
    prospect.next_action = next_action or None
    prospect.next_action_date = date.fromisoformat(next_action_date) if next_action_date else None
    prospect.notes = notes or None
    prospect.location_id = location_id or None
    db.commit()
    return RedirectResponse(url="/sales/", status_code=303)


@router.get("/{prospect_id}", response_class=HTMLResponse)
def prospect_detail(
    prospect_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    prospect = db.get(Prospect, prospect_id)
    if not prospect:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request,
        "sales/detail.html",
        {"active_nav": "sales", "prospect": prospect, "stages": PIPELINE_STAGES},
    )


@router.post("/{prospect_id}/stage", response_class=HTMLResponse)
def prospect_advance_stage(prospect_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    prospect = db.get(Prospect, prospect_id)
    if not prospect:
        return Response(status_code=404)
    idx = (
        PIPELINE_STAGES.index(prospect.pipeline_stage)
        if prospect.pipeline_stage in PIPELINE_STAGES
        else 0
    )
    if idx < len(PIPELINE_STAGES) - 1:
        prospect.pipeline_stage = PIPELINE_STAGES[idx + 1]
        db.commit()
    return RedirectResponse(url="/sales/", status_code=303)


@router.post("/{prospect_id}/stage/back", response_class=HTMLResponse)
def prospect_retreat_stage(prospect_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    prospect = db.get(Prospect, prospect_id)
    if not prospect:
        return Response(status_code=404)
    idx = (
        PIPELINE_STAGES.index(prospect.pipeline_stage)
        if prospect.pipeline_stage in PIPELINE_STAGES
        else 0
    )
    if idx > 0:
        prospect.pipeline_stage = PIPELINE_STAGES[idx - 1]
        db.commit()
    return RedirectResponse(url="/sales/", status_code=303)


@router.post("/{prospect_id}/log", response_class=HTMLResponse)
def prospect_log_outreach(
    prospect_id: int,
    request: Request,
    channel: str = Form(...),
    subject_or_summary: str = Form(""),
    outcome: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    log = OutreachLog(
        prospect_id=prospect_id,
        channel=channel,
        contacted_at=datetime.now(),
        subject_or_summary=subject_or_summary or None,
        outcome=outcome or None,
        notes=notes or None,
    )
    db.add(log)
    db.commit()
    return RedirectResponse(url=f"/sales/{prospect_id}", status_code=303)


@router.delete("/{prospect_id}", response_class=HTMLResponse)
def prospect_delete(
    prospect_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    prospect = db.get(Prospect, prospect_id)
    if prospect:
        db.delete(prospect)
        db.commit()
    if request.headers.get("HX-Request"):
        return HTMLResponse(content="", status_code=200, headers={"HX-Redirect": "/sales/"})
    return HTMLResponse(content="", status_code=200)
