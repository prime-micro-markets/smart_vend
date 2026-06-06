from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.sales import OutreachLog, Prospect


def _make_prospect(db: Session, **kwargs) -> Prospect:
    defaults = {
        "company_name": "Bay Fitness",
        "city": "Panama City",
        "pipeline_stage": "lead",
    }
    defaults.update(kwargs)
    p = Prospect(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_sales_index_empty(client: TestClient) -> None:
    resp = client.get("/sales/")
    assert resp.status_code == 200
    assert "Sales" in resp.text


def test_prospect_create(client: TestClient, db: Session) -> None:
    resp = client.post(
        "/sales/",
        data={"company_name": "Bay Hotel", "city": "Panama City"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.query(Prospect).count() == 1
    p = db.query(Prospect).first()
    assert p is not None
    assert p.company_name == "Bay Hotel"
    assert p.pipeline_stage == "lead"


def test_sales_index_shows_prospects(client: TestClient, db: Session) -> None:
    _make_prospect(db)
    resp = client.get("/sales/")
    assert resp.status_code == 200
    assert "Bay Fitness" in resp.text


def test_prospect_detail(client: TestClient, db: Session) -> None:
    p = _make_prospect(db)
    resp = client.get(f"/sales/{p.id}")
    assert resp.status_code == 200
    assert "Bay Fitness" in resp.text


def test_prospect_advance_stage(client: TestClient, db: Session) -> None:
    p = _make_prospect(db, pipeline_stage="lead")
    resp = client.post(f"/sales/{p.id}/stage", follow_redirects=False)
    assert resp.status_code == 303
    db.refresh(p)
    assert p.pipeline_stage == "contacted"


def test_prospect_advance_stage_at_end(client: TestClient, db: Session) -> None:
    # "lost" is the last stage — advancing should not change it
    p = _make_prospect(db, pipeline_stage="lost")
    client.post(f"/sales/{p.id}/stage", follow_redirects=False)
    db.refresh(p)
    assert p.pipeline_stage == "lost"


def test_prospect_log_outreach(client: TestClient, db: Session) -> None:
    p = _make_prospect(db)
    resp = client.post(
        f"/sales/{p.id}/log",
        data={"channel": "email", "outcome": "no_response"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.query(OutreachLog).count() == 1
    log = db.query(OutreachLog).first()
    assert log is not None
    assert log.channel == "email"
    assert log.prospect_id == p.id


def test_prospect_delete(client: TestClient, db: Session) -> None:
    p = _make_prospect(db)
    resp = client.delete(f"/sales/{p.id}")
    assert resp.status_code == 200
    assert db.query(Prospect).count() == 0


def test_prospect_404(client: TestClient) -> None:
    assert client.get("/sales/9999").status_code == 404


def test_prospect_card_modal_shows_outreach_section(client: TestClient, db: Session) -> None:
    p = _make_prospect(db)
    resp = client.get(f"/sales/{p.id}/card")
    assert resp.status_code == 200
    assert "Outreach" in resp.text
    assert "Draft outreach email (AI)" in resp.text
    assert "Not yet contacted" in resp.text
