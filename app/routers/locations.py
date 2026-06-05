from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.location import Location, Machine
from app.views import templates

router = APIRouter(prefix="/locations", tags=["locations"])


@router.get("/", response_class=HTMLResponse)
def locations_index(
    request: Request,
    status: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    from app.models.sales import Prospect

    query = db.query(Location)
    if status:
        query = query.filter(Location.status == status)
    locations = query.order_by(Location.name).all()

    # Show pipeline leads unless filtering by a location-specific status
    pipeline_leads: list[Prospect] = []
    if not status or status in ("prospect", ""):
        pipeline_leads = (
            db.query(Prospect)
            .filter(Prospect.pipeline_stage.notin_(["signed", "lost"]))
            .order_by(Prospect.company_name)
            .all()
        )

    return templates.TemplateResponse(
        request,
        "locations/index.html",
        {
            "active_nav": "locations",
            "locations": locations,
            "status_filter": status,
            "pipeline_leads": pipeline_leads,
        },
    )


@router.get("/map-data")
def locations_map_data(db: Session = Depends(get_db)) -> JSONResponse:
    from app.models.sales import Prospect

    locs = db.query(Location).all()
    leads = (
        db.query(Prospect)
        .filter(Prospect.pipeline_stage.notin_(["signed", "lost"]))
        .order_by(Prospect.company_name)
        .all()
    )

    def loc_addr(l: Location) -> str:
        parts = [l.address, l.city, f"{l.state} {l.zip_code or ''}".strip()]
        return ", ".join(p for p in parts if p)

    def lead_addr(p: Prospect) -> str:
        parts = [p.address, p.city, "FL"]
        return ", ".join(p for p in parts if p)

    return JSONResponse(
        {
            "locations": [
                {
                    "id": l.id,
                    "name": l.name,
                    "address": loc_addr(l),
                    "status": l.status,
                    "venue_type": l.venue_type or "",
                    "contact_name": l.contact_name or "",
                    "url": f"/locations/{l.id}",
                }
                for l in locs
            ],
            "leads": [
                {
                    "id": p.id,
                    "name": p.company_name,
                    "address": lead_addr(p),
                    "stage": p.pipeline_stage,
                    "venue_type": p.venue_type or "",
                    "contact_name": p.contact_name or "",
                    "url": f"/sales/{p.id}",
                }
                for p in leads
            ],
        }
    )


@router.get("/new", response_class=HTMLResponse)
def location_new_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "locations/_location_form.html", {"location": None})


@router.post("/", response_class=HTMLResponse)
def location_create(
    request: Request,
    name: str = Form(...),
    address: str = Form(""),
    city: str = Form("Panama City"),
    state: str = Form("FL"),
    zip_code: str = Form(""),
    venue_type: str = Form(""),
    foot_traffic_estimate: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    status: str = Form("prospect"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    location = Location(
        name=name,
        address=address or None,
        city=city,
        state=state,
        zip_code=zip_code or None,
        venue_type=venue_type or None,
        foot_traffic_estimate=foot_traffic_estimate or None,
        contact_name=contact_name or None,
        contact_email=contact_email or None,
        contact_phone=contact_phone or None,
        status=status,
        notes=notes or None,
    )
    db.add(location)
    db.commit()
    return RedirectResponse(url="/locations/", status_code=303)


@router.get("/{location_id}", response_class=HTMLResponse)
def location_detail(
    location_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    from app.models.sales import Prospect

    location = db.get(Location, location_id)
    if not location:
        return Response(status_code=404)
    machines = db.query(Machine).filter(Machine.location_id == location_id).all()
    linked = db.query(Prospect).filter(Prospect.location_id == location_id).all()
    prospects_by_group = {
        "Prospects": [p for p in linked if p.pipeline_stage in ("lead", "contacted")],
        "Pending": [p for p in linked if p.pipeline_stage == "proposal"],
        "Active": [p for p in linked if p.pipeline_stage == "signed"],
        "Other": [
            p for p in linked if p.pipeline_stage not in ("lead", "contacted", "proposal", "signed")
        ],
    }
    unlinked = (
        db.query(Prospect)
        .filter(Prospect.location_id.is_(None))
        .order_by(Prospect.company_name)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "locations/detail.html",
        {
            "active_nav": "locations",
            "location": location,
            "machines": machines,
            "prospects_by_group": prospects_by_group,
            "unlinked_prospects": unlinked,
        },
    )


@router.post("/{location_id}/link-prospect", response_class=HTMLResponse)
def location_link_prospect(
    location_id: int,
    prospect_id: int = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    from app.models.sales import Prospect

    prospect = db.get(Prospect, prospect_id)
    if prospect:
        prospect.location_id = location_id
        db.commit()
    return RedirectResponse(url=f"/locations/{location_id}", status_code=303)


@router.post("/{location_id}/unlink-prospect/{prospect_id}", response_class=HTMLResponse)
def location_unlink_prospect(
    location_id: int,
    prospect_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    from app.models.sales import Prospect

    prospect = db.get(Prospect, prospect_id)
    if prospect and prospect.location_id == location_id:
        prospect.location_id = None
        db.commit()
    return RedirectResponse(url=f"/locations/{location_id}", status_code=303)


@router.get("/{location_id}/edit", response_class=HTMLResponse)
def location_edit_form(
    location_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    location = db.get(Location, location_id)
    if not location:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request, "locations/_location_form.html", {"location": location}
    )


@router.post("/{location_id}", response_class=HTMLResponse)
def location_update(
    location_id: int,
    request: Request,
    name: str = Form(...),
    address: str = Form(""),
    city: str = Form("Panama City"),
    state: str = Form("FL"),
    zip_code: str = Form(""),
    venue_type: str = Form(""),
    foot_traffic_estimate: str = Form(""),
    foot_traffic_notes: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    contract_status: str = Form("none"),
    status: str = Form("prospect"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    location = db.get(Location, location_id)
    if not location:
        return Response(status_code=404)
    location.name = name
    location.address = address or None
    location.city = city
    location.state = state
    location.zip_code = zip_code or None
    location.venue_type = venue_type or None
    location.foot_traffic_estimate = foot_traffic_estimate or None
    location.foot_traffic_notes = foot_traffic_notes or None
    location.contact_name = contact_name or None
    location.contact_email = contact_email or None
    location.contact_phone = contact_phone or None
    location.contract_status = contract_status
    location.status = status
    location.notes = notes or None
    db.commit()
    return RedirectResponse(url=f"/locations/{location_id}", status_code=303)


@router.delete("/{location_id}", response_class=HTMLResponse)
def location_delete(location_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    location = db.get(Location, location_id)
    if location:
        db.delete(location)
        db.commit()
    return RedirectResponse(url="/locations/", status_code=303)
