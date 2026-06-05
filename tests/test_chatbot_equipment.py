"""The public chatbot must be able to reference real equipment specs.

Regression coverage for the bug where the system prompt carried no catalog data,
so questions like "how many drinks can a machine hold?" went unanswered.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.equipment import EquipmentUnit
from app.services.cs_chatbot_agent import (
    build_chatbot_system_prompt,
    build_equipment_knowledge,
)


def _unit(db: Session, **kw) -> EquipmentUnit:
    defaults = {
        "manufacturer": "HAHA Vending",
        "product_name": "Mini 360C",
        "equipment_type": "smart_cooler",
        "status": "active",
    }
    defaults.update(kw)
    u = EquipmentUnit(**defaults)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_knowledge_empty_when_no_units(db: Session) -> None:
    assert build_equipment_knowledge(db) == ""


def test_knowledge_includes_capacity(db: Session) -> None:
    _unit(db, capacity_units=300, capacity_cu_ft=12.5)
    knowledge = build_equipment_knowledge(db)
    assert "HAHA Vending Mini 360C" in knowledge
    assert "AI Smart Cooler" in knowledge
    assert "300" in knowledge  # the drink/product count
    assert "12.5 cu ft" in knowledge


def test_knowledge_excludes_archived(db: Session) -> None:
    _unit(db, product_name="Active Cooler", capacity_units=200)
    _unit(db, product_name="Old Cooler", status="archived", capacity_units=999)
    knowledge = build_equipment_knowledge(db)
    assert "Active Cooler" in knowledge
    assert "Old Cooler" not in knowledge


def test_knowledge_omits_missing_specs(db: Session) -> None:
    # A bare unit with no specs should not emit empty fragments or fake numbers.
    _unit(db, product_name="Bare Unit")
    knowledge = build_equipment_knowledge(db)
    assert "Bare Unit" in knowledge
    assert "holds ~" not in knowledge
    assert "cu ft" not in knowledge


def test_pricing_never_surfaced(db: Session) -> None:
    # Equipment is installed at no cost to the host; the bot must not quote prices.
    _unit(db, product_name="Ranged", capacity_units=250, price_low=3000, price_high=4500)
    _unit(
        db,
        product_name="Market",
        equipment_type="micro_market",
        price_low=8000,
        price_is_starting=True,
    )
    knowledge = build_equipment_knowledge(db)
    # Specs still surface...
    assert "Ranged" in knowledge
    assert "holds ~250" in knowledge
    # ...but no dollar figures or price language appears anywhere.
    assert "$" not in knowledge
    assert "3,000" not in knowledge
    assert "8,000" not in knowledge
    assert "price:" not in knowledge


def test_no_cost_and_appointment_messaging(db: Session) -> None:
    _unit(db, capacity_units=300)
    knowledge = build_equipment_knowledge(db)
    assert "NO COST" in knowledge
    assert "on-site evaluation" in knowledge
    assert "appointment" in knowledge.lower()


def test_system_prompt_embeds_catalog(db: Session) -> None:
    _unit(db, capacity_units=300)
    prompt = build_chatbot_system_prompt(db, include_tools=False)
    assert "EQUIPMENT CATALOG" in prompt
    assert "holds ~300 items/products" in prompt
