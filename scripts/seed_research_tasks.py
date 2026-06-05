"""Seed / update research tasks from research_tracker.md into the database.

Upsert behaviour (safe to re-run at any time):
  - New tasks (task_number not in DB) are inserted.
  - Existing tasks have what / why / how_source / priority updated from the file.
  - status is NEVER overwritten (preserves in-progress / done / blocked work).
  - notes is preserved if the DB row already has a value; populated from the
    markdown only when the DB notes field is currently NULL.

Usage:
    python scripts/seed_research_tasks.py
    python scripts/seed_research_tasks.py "/path/to/research_tracker.md"
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import Session

from app.database import Base, engine
from app.models.research import ResearchTask  # noqa: F401

DEFAULT_MD_PATH = Path("G:/My Drive/Smart Cooler Vending/research_tracker.md")

SECTION_NAMES: dict[int, str] = {
    1: "Market Validation",
    2: "Equipment Vendor Due Diligence",
    3: "Legal, Regulatory, Compliance",
    4: "Financial Modeling & Capital Strategy",
    5: "Sales Pipeline & First Contracts",
    6: "Technology & Web",
    7: "Operational Setup",
    8: "Strategic Decisions Pending",
    9: "Industry Knowledge to Build",
}

STATUS_MAP: dict[str, str] = {
    "🔲": "not_started",
    "🟡": "in_progress",
    "🔴": "blocked",
    "✅": "done",
    "not started": "not_started",
    "in progress": "in_progress",
    "blocked": "blocked",
    "done": "done",
}


def _parse_status(raw: str) -> str:
    raw = raw.strip()
    return STATUS_MAP.get(raw, STATUS_MAP.get(raw.lower(), "not_started"))


def _parse_priority(raw: str) -> str:
    val = raw.strip().lower()
    return val if val in ("high", "medium", "low") else "medium"


def _split_row(line: str) -> list[str]:
    parts = line.strip().strip("|").split("|")
    return [p.strip() for p in parts]


def _is_separator(line: str) -> bool:
    return bool(re.match(r"^\|[-| :]+\|$", line.strip()))


def parse_markdown(md_path: Path) -> list[dict]:
    tasks: list[dict] = []
    current_section = 0
    section_task_counts: dict[int, int] = {}
    in_table = False
    has_priority_col = False

    lines = md_path.read_text(encoding="utf-8").splitlines()

    for line in lines:
        sec_match = re.match(r"^#{1,3}\s+Section\s+(\d+)", line, re.IGNORECASE)
        if not sec_match:
            sec_match = re.match(r"^#{1,3}\s+(\d+)\.", line)
        if sec_match:
            current_section = int(sec_match.group(1))
            in_table = False
            has_priority_col = False
            continue

        if current_section == 0:
            continue

        # Section 9 — bullet list
        if current_section == 9:
            bullet = re.match(r"^[-*]\s+\*\*(.+?)\*\*[:\s]*(.*)", line)
            if bullet:
                n = section_task_counts.get(9, 0) + 1
                section_task_counts[9] = n
                tasks.append(
                    {
                        "task_number": f"9.{n}",
                        "section": 9,
                        "section_name": SECTION_NAMES[9],
                        "what": bullet.group(1).strip(),
                        "why": None,
                        "how_source": bullet.group(2).strip() or None,
                        "owner": None,
                        "due_date_raw": None,
                        "status": "not_started",
                        "priority": "medium",
                        "notes": None,
                        "is_strategic_decision": False,
                    }
                )
            continue

        # Table header — detect columns (match "What" as a full cell, not a substring)
        if line.strip().startswith("|") and re.search(r"\|\s*What\s*\|", line):
            in_table = True
            has_priority_col = bool(re.search(r"\|\s*Priority\s*\|", line, re.IGNORECASE))
            continue

        if _is_separator(line):
            continue

        if not in_table or not line.strip().startswith("|"):
            continue

        cells = _split_row(line)

        if current_section == 8:
            # Columns: # | Decision | Inputs Needed | Target Resolution
            if len(cells) < 2:
                continue
            n = section_task_counts.get(8, 0) + 1
            section_task_counts[8] = n
            task_num = cells[0] if cells[0] else f"8.{n}"
            decision = cells[1] if len(cells) > 1 else ""
            inputs_needed = cells[2] if len(cells) > 2 else ""
            target_res = cells[3] if len(cells) > 3 else ""
            if not decision:
                continue
            tasks.append(
                {
                    "task_number": task_num if re.match(r"\d+\.\d+", task_num) else f"8.{n}",
                    "section": 8,
                    "section_name": SECTION_NAMES[8],
                    "what": decision,
                    "why": None,
                    "how_source": inputs_needed or None,
                    "owner": None,
                    "due_date_raw": target_res or None,
                    "status": "not_started",
                    "priority": "medium",
                    "notes": None,
                    "is_strategic_decision": True,
                }
            )
        else:
            # Sections 1–7
            # With priority col:
            #   # | What | Priority | Why | How/Source | Owner/Due | Status | Notes
            # Without priority col:
            #   # | What | Why | How/Source | Owner/Due | Status | Notes
            if len(cells) < 2:
                continue

            task_num_raw = cells[0]

            if has_priority_col:
                what = cells[1] if len(cells) > 1 else ""
                priority = _parse_priority(cells[2]) if len(cells) > 2 else "medium"
                why = cells[3] if len(cells) > 3 else ""
                how_source = cells[4] if len(cells) > 4 else ""
                owner_due = cells[5] if len(cells) > 5 else ""
                status_raw = cells[6] if len(cells) > 6 else ""
                notes = cells[7] if len(cells) > 7 else ""
            else:
                what = cells[1] if len(cells) > 1 else ""
                priority = "medium"
                why = cells[2] if len(cells) > 2 else ""
                how_source = cells[3] if len(cells) > 3 else ""
                owner_due = cells[4] if len(cells) > 4 else ""
                status_raw = cells[5] if len(cells) > 5 else ""
                notes = cells[6] if len(cells) > 6 else ""

            if not what or what.lower() in ("what", "task", "#"):
                continue

            n = section_task_counts.get(current_section, 0) + 1
            section_task_counts[current_section] = n
            task_number = (
                task_num_raw if re.match(r"\d+\.\d+", task_num_raw) else f"{current_section}.{n}"
            )

            tasks.append(
                {
                    "task_number": task_number,
                    "section": current_section,
                    "section_name": SECTION_NAMES.get(
                        current_section, f"Section {current_section}"
                    ),
                    "what": what,
                    "why": why or None,
                    "how_source": how_source or None,
                    "owner": owner_due or None,
                    "due_date_raw": None,
                    "status": _parse_status(status_raw),
                    "priority": priority,
                    "notes": notes or None,
                    "is_strategic_decision": False,
                }
            )

    return tasks


def seed(md_path: Path) -> None:
    Base.metadata.create_all(bind=engine)
    tasks = parse_markdown(md_path)
    print(f"Parsed {len(tasks)} tasks from {md_path.name}")

    inserted = updated = 0
    with Session(engine) as db:
        existing: dict[str, ResearchTask] = {t.task_number: t for t in db.query(ResearchTask).all()}
        for t in tasks:
            row = existing.get(t["task_number"])
            if row is None:
                db.add(ResearchTask(**t))
                inserted += 1
            else:
                # Update content fields; never touch status
                row.what = t["what"]
                row.why = t["why"]
                row.how_source = t["how_source"]
                row.owner = t["owner"]
                row.priority = t["priority"]
                row.is_strategic_decision = t["is_strategic_decision"]
                # Only populate notes if the DB row has none
                if row.notes is None and t["notes"]:
                    row.notes = t["notes"]
                updated += 1
        db.commit()

    print(f"  Inserted: {inserted}  Updated: {updated}")


if __name__ == "__main__":
    md_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MD_PATH
    if not md_path.exists():
        print(f"ERROR: File not found: {md_path}")
        sys.exit(1)
    seed(md_path)
