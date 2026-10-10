"""Tests for §13 steps 19–20 and §16: turn views & image task queue.

Covers:
 * the dashboard block has exactly the fields of §16.1 (turn, era, elapsed
   game time, current year, projected next-turn span);
 * the era-change warning line appears when the interval shrinks (§16.1);
 * image-generation tasks are queued per turn (hero + one vignette per
   applied revision) and reference only published view data;
 * the REST API publishes the report with view + image_tasks (§13 step 22)
   and GET /worlds/{id}/turns/report returns it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.api import WorldServer
from poem.chronology import Civilization, WorldChronology
from poem.core import (
    CanonicalLaw,
    CanonicalLawChange,
    Law,
    LawUserAction,
)
from poem.laws import LawStore
from poem.turn import TurnEngine
from poem.views import build_turn_view, queue_image_tasks, render_dashboard_text

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


def make_engine(progress=0.25):
    store = LawStore()
    civ = Civilization(id="c1", name="Rivers", progress=progress,
                      annual_production_rate=100.0)
    store.add_law(Law(
        id="law-tax", civilization_id="c1", title="Head tax",
        raw_text="Every settlement pays 1 unit per hearth.",
        canonical=CanonicalLaw(kind="tax"), complexity=1.5, tradition=0.2))
    chrono = WorldChronology("w1")
    engine = TurnEngine("w1", store, chrono, [civ])
    return store, chrono, engine, civ


# ---------------------------------------------------------------------------
# §16.1 dashboard view
# ---------------------------------------------------------------------------

def test_dashboard_view_has_all_fields_of_section_16_1():
    store, chrono, engine, civ = make_engine()
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    report = engine.run_turn()

    d = report.view["dashboard"]
    assert set(d) >= {"turn", "era", "years_since_last_turn",
                      "current_year", "next_turn_span_years"}
    assert d["turn"] == 1
    # classical era: 25 years per turn on standard speed
    assert d["years_since_last_turn"] == 25.0
    assert d["current_year"] == 25.0
    assert d["era"] == "Классическая"          # localized label, §7.3
    assert d["next_turn_span_years"] == 25.0

    text = render_dashboard_text(report.view)
    assert "Ход: 1" in text
    assert "Прошло времени с прошлого хода: 25 лет" in text
    assert "Следующий ход охватит примерно: 25 лет" in text


def test_view_includes_laws_economy_and_admin_costs():
    store, chrono, engine, civ = make_engine()
    rev = store.record_revision(
        "law-tax", "c1", LawUserAction.AMEND_LAW, "Exempt small settlements.",
        CanonicalLawChange(action=LawUserAction.AMEND_LAW,
                           new_canonical=CanonicalLaw(kind="tax")),
        now=DAY.replace(hour=10))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    report = engine.run_turn()

    view = report.view
    assert rev.id in view["applied_revisions"]
    law_entry = next(l for l in view["laws"] if l["id"] == "law-tax")
    assert law_entry["status"] == "active"
    assert law_entry["age_years"] == 25.0            # §11.4 scaled by game time
    assert view["admin_costs"]["c1"] > 0.0           # §5 charge surfaced to UI
    econ = next(e for e in view["economy"] if e["civilization_id"] == "c1")
    assert econ["production"] == 2500.0              # 100/yr * 25 y


def test_era_change_notice_shrinks_next_span_in_view():
    """§16.1 'Внимание' line: after a jump into a later era the projected
    next-turn span must be smaller than the span just simulated."""
    store, chrono, engine, civ = make_engine(progress=0.10)   # ancient: 40 y
    # research is strong enough to cross the classical threshold (0.20):
    # 50/yr * 40y = 2000 progress points * 1e-4 = +0.20 -> progress 0.30
    civ.annual_research_rate = 50.0
    civ.research_to_progress = 1e-4
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    report = engine.run_turn()

    d = report.view["dashboard"]
    assert d["years_since_last_turn"] == 40.0
    assert any(n.startswith("Внимание") for n in report.view["notices"])
    assert d["next_turn_span_years"] < d["years_since_last_turn"]
    text = render_dashboard_text(report.view)
    assert "Внимание:" in text


# ---------------------------------------------------------------------------
# §13 step 20 — image generation queue
# ---------------------------------------------------------------------------

def test_image_tasks_queued_hero_plus_one_per_applied_revision():
    store, chrono, engine, civ = make_engine()
    r1 = store.record_revision(
        "law-tax", "c1", LawUserAction.CLARIFY_LAW, "Clarify wording.",
        CanonicalLawChange(action=LawUserAction.CLARIFY_LAW),
        now=DAY.replace(hour=9))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    report = engine.run_turn()

    tasks = report.image_tasks
    kinds = [t["payload"]["kind"] for t in tasks]
    assert kinds.count("hero") == 1
    assert kinds.count("law_vignette") == len(report.applied_revisions) == 1
    hero = next(t for t in tasks if t["payload"]["kind"] == "hero")
    # task references ONLY published view data (reproducible rendering)
    assert hero["payload"]["era"] == report.view["dashboard"]["era_value"]
    assert hero["payload"]["year"] == report.view["dashboard"]["current_year"]
    assert all(t["status"] == "queued" for t in tasks)


def test_no_revisions_still_queues_single_hero_task():
    store, chrono, engine, civ = make_engine()
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    report = engine.run_turn()
    assert report.applied_revisions == []
    assert len(report.image_tasks) == 1
    assert report.image_tasks[0]["payload"]["kind"] == "hero"


# ---------------------------------------------------------------------------
# §13 step 22 — publication through the REST API
# ---------------------------------------------------------------------------

def test_api_publishes_report_with_view_and_image_tasks():
    srv = WorldServer("w1")
    srv.add_civilization(Civilization(id="c1", name="Rivers", progress=0.25,
                                      annual_production_rate=100.0))
    resp = srv.handle("POST", "/api/v1/laws", {
        "id": "law-tax",
        "civilization_id": "c1", "title": "Head tax",
        "raw_text": "Every settlement pays 1 unit per hearth.",
        "canonical": {"kind": "tax"},
    })
    assert resp["status"] == 200, resp
    resp = srv.handle("POST", "/api/v1/laws/law-tax/revisions", {
        "civilization_id": "c1", "action": "AmendLaw",
        "raw_text": "Exempt small settlements.",
        "canonical_change": {"action": "AmendLaw",
                             "new_canonical": {"kind": "tax"}},
        "now": "2026-10-10T10:00:00+00:00",
    })
    assert resp["status"] == 200, resp
    resp = srv.handle("POST", "/api/v1/worlds/w1/turns/open", {
        "started_at": "2026-10-10T00:00:00+00:00",
        "simulation_cutoff_at": "2026-10-10T23:00:00+00:00",
    })
    assert resp["status"] == 200, resp
    run_body = srv.handle("POST", "/api/v1/worlds/w1/turns/run", {})
    assert run_body["status"] == 200, run_body
    assert run_body["view"]["dashboard"]["years_since_last_turn"] == 25.0
    assert len(run_body["image_tasks"]) >= 1

    # the published report is retrievable afterwards (§16 client fetch)
    fetched = srv.handle("GET", "/api/v1/worlds/w1/turns/report", None)
    assert fetched["status"] == 200
    assert fetched["report"]["turn_number"] == run_body["turn_number"]
    assert fetched["report"]["view"] == run_body["view"]
