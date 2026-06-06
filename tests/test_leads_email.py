from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.agent import AgentJob
from app.models.sales import OutreachLog, Prospect
from app.services.agent import _append_ai_findings_to_notes


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


def _make_email_job(db: Session, prospect: Prospect, **kwargs) -> AgentJob:
    defaults = {
        "job_type": "email_draft",
        "status": "done",
        "prospect_id": prospect.id,
        "draft_subject": "Hello",
        "draft_body": "A drafted message body.",
    }
    defaults.update(kwargs)
    job = AgentJob(**defaults)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


# ── Part 1: AI research enhances notes instead of overwriting ────────────────


def test_append_ai_findings_preserves_existing_notes() -> None:
    p = Prospect(company_name="Bay Fitness", city="Panama City", notes="Operator: call after 5pm.")
    _append_ai_findings_to_notes(p, {"summary": "Busy gym near the marina.", "employees": "15"})
    assert "Operator: call after 5pm." in p.notes
    assert "Busy gym near the marina." in p.notes
    assert "Est. employees: 15" in p.notes
    assert "AI research" in p.notes


def test_append_ai_findings_accumulates_across_runs() -> None:
    p = Prospect(company_name="Bay Fitness", city="Panama City")
    _append_ai_findings_to_notes(p, {"summary": "First pass."})
    _append_ai_findings_to_notes(p, {"summary": "Second pass."})
    assert "First pass." in p.notes
    assert "Second pass." in p.notes
    assert p.notes.count("AI research") == 2


def test_append_ai_findings_noop_on_empty() -> None:
    p = Prospect(company_name="Bay Fitness", city="Panama City", notes="keep me")
    _append_ai_findings_to_notes(p, {})
    _append_ai_findings_to_notes(p, {"summary": "", "employees": ""})
    assert p.notes == "keep me"


# ── Part 5: real send advances to "contacted"; preview does not ──────────────


def test_send_email_advances_lead_to_contacted(
    client: TestClient, db: Session, monkeypatch
) -> None:
    monkeypatch.setattr("app.routers.leads.email_sender.send_email", lambda **kw: None)
    p = _make_prospect(db, contact_email="owner@bayfitness.com", pipeline_stage="lead")
    job = _make_email_job(db, p)
    resp = client.post(
        f"/leads/jobs/{job.id}/send",
        data={"to_email": p.contact_email, "subject": "Hi", "body": "Body"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(p)
    assert p.pipeline_stage == "contacted"
    assert db.query(OutreachLog).filter_by(prospect_id=p.id, outcome="sent").count() == 1


def test_send_email_does_not_regress_later_stage(
    client: TestClient, db: Session, monkeypatch
) -> None:
    monkeypatch.setattr("app.routers.leads.email_sender.send_email", lambda **kw: None)
    p = _make_prospect(db, contact_email="owner@bayfitness.com", pipeline_stage="proposal")
    job = _make_email_job(db, p)
    client.post(
        f"/leads/jobs/{job.id}/send",
        data={"to_email": p.contact_email, "subject": "Hi", "body": "Body"},
        follow_redirects=False,
    )
    db.refresh(p)
    assert p.pipeline_stage == "proposal"


def test_preview_send_does_not_advance_stage(client: TestClient, db: Session) -> None:
    p = _make_prospect(db, contact_email="owner@bayfitness.com", pipeline_stage="lead")
    job = _make_email_job(db, p, preview_mode=True)
    resp = client.post(
        f"/leads/jobs/{job.id}/send",
        data={"to_email": p.contact_email, "subject": "Hi", "body": "Body"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(p)
    assert p.pipeline_stage == "lead"
    assert db.query(OutreachLog).filter_by(prospect_id=p.id, outcome="preview").count() == 1


# ── Part 2: Email Outreach tab + inline HTMX draft ───────────────────────────


def test_email_tab_renders_prospect_and_badges(client: TestClient, db: Session) -> None:
    p = _make_prospect(db, contact_email="owner@bayfitness.com")
    _make_email_job(db, p)
    resp = client.get("/leads/?tab=email")
    assert resp.status_code == 200
    assert "Email Outreach" in resp.text
    assert "Bay Fitness" in resp.text
    assert "Draft ready" in resp.text


def test_draft_email_htmx_returns_inline_card(client: TestClient, db: Session, monkeypatch) -> None:
    # Don't actually run the background draft job against the real engine.
    monkeypatch.setattr("app.routers.leads.agent.run_email_draft_job", lambda job_id: None)
    p = _make_prospect(db)
    resp = client.post(
        f"/leads/prospects/{p.id}/draft",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "job-status-card" in resp.text
    assert db.query(AgentJob).filter_by(job_type="email_draft", prospect_id=p.id).count() == 1
