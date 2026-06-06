from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.location import Location, Machine


def _make_location(db: Session, **kwargs) -> Location:
    defaults = {
        "name": "Golds Gym",
        "city": "Panama City",
        "state": "FL",
        "status": "prospect",
    }
    defaults.update(kwargs)
    loc = Location(**defaults)
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc


def test_locations_index_empty(client: TestClient) -> None:
    resp = client.get("/locations/")
    assert resp.status_code == 200
    assert "Locations" in resp.text


def test_location_create(client: TestClient, db: Session) -> None:
    resp = client.post(
        "/locations/",
        data={"name": "Airport Hotel", "city": "Panama City", "state": "FL"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.query(Location).count() == 1
    loc = db.query(Location).first()
    assert loc is not None
    assert loc.name == "Airport Hotel"
    assert loc.status == "prospect"


def test_locations_index_shows_locations(client: TestClient, db: Session) -> None:
    _make_location(db)
    resp = client.get("/locations/")
    assert resp.status_code == 200
    assert "Golds Gym" in resp.text


def test_locations_index_redirects_to_sales_tab(client: TestClient) -> None:
    # Locations now live as a tab on the Sales Pipeline page; the old URL is a
    # permanent shortcut into that tab.
    resp = client.get("/locations/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/sales/?tab=locations"


def test_locations_status_filter(client: TestClient, db: Session) -> None:
    # The Locations tab renders every location card (carrying a data-status for
    # the client-side filter pills) rather than server-side filtering by status.
    _make_location(db, name="Active Spot", status="active")
    _make_location(db, name="Prospect Spot", status="prospect")
    resp = client.get("/locations/")  # follows redirect into the sales tab
    assert resp.status_code == 200
    assert "Active Spot" in resp.text
    assert "Prospect Spot" in resp.text
    assert 'data-status="active"' in resp.text


def test_location_detail(client: TestClient, db: Session) -> None:
    loc = _make_location(db)
    resp = client.get(f"/locations/{loc.id}")
    assert resp.status_code == 200
    assert "Golds Gym" in resp.text


def test_location_edit_form(client: TestClient, db: Session) -> None:
    loc = _make_location(db)
    resp = client.get(f"/locations/{loc.id}/edit")
    assert resp.status_code == 200
    assert "Golds Gym" in resp.text


def test_location_update(client: TestClient, db: Session) -> None:
    loc = _make_location(db)
    resp = client.post(
        f"/locations/{loc.id}",
        data={
            "name": "Updated Gym",
            "city": "Panama City",
            "state": "FL",
            "status": "active",
            "contract_status": "signed",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(loc)
    assert loc.name == "Updated Gym"
    assert loc.status == "active"


def test_location_delete(client: TestClient, db: Session) -> None:
    loc = _make_location(db)
    resp = client.delete(f"/locations/{loc.id}", follow_redirects=False)
    assert resp.status_code == 303
    assert db.query(Location).count() == 0


def test_location_detail_shows_machines(client: TestClient, db: Session) -> None:
    loc = _make_location(db)
    machine = Machine(
        serial_number="SN-001",
        vendor="Crane",
        status="deployed",
        location_id=loc.id,
    )
    db.add(machine)
    db.commit()
    resp = client.get(f"/locations/{loc.id}")
    assert resp.status_code == 200
    assert "SN-001" in resp.text


def test_location_404(client: TestClient) -> None:
    assert client.get("/locations/9999").status_code == 404
