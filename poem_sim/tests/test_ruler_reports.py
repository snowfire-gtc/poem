"""Tests for §13 step 22: the ruler's literary turn report.

After every server-side turn calculation each civilization receives a
report about the elapsed interval *from the point of view of its ruler*:
wars, scientific/cultural breakthroughs, geographical discoveries, folk
unrest and lawcraft are wrapped into artistic/literary pieces (epics,
laments, verses, chronicle prose, warnings).

Covers:
 * ``build_ruler_report`` output structure and the quiet-interval fallback;
 * end-to-end engine integration — sections appear for wars / unrest /
   folklore / applied revisions;
 * determinism (§4.1): same finalized inputs → byte-identical report;
 * API publication — POST /turns/run and GET /turns/report ship
   ``ruler_reports`` both at top level and inside ``view``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.api import WorldServer
from poem.chronicle_report import build_ruler_report
from poem.chronology import Civilization, WorldChronology
from poem.core import (
    CanonicalLaw,
    CanonicalLawChange,
    FolkloreItem,
    Law,
    LawUserAction,
    Prejudice,
    War,
)
from poem.laws import LawStore
from poem.turn import TurnEngine

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


def _minimal_view():
    return {
        "dashboard": {"turn": 3, "current_year": 850, "era_value": "classical"},
        "economy": [],
        "wars": [],
        "events": [],
        "laws": [],
        "folklore": [],
        "prejudices": [],
        "diplomacy": [],
    }


# ---------------------------------------------------------------------------
# pure generator: structure & fallback
# ---------------------------------------------------------------------------

def test_build_ruler_report_structure_and_quiet_fallback():
    view = _minimal_view()
    report = {"years_elapsed": 25, "_civ_names": {}, "_war_parties": {},
              "applied_revisions": [], "_revisions_meta": [],
              "_folklore_meta": []}
    out = build_ruler_report(view, report, [{"id": "c1", "name": "Rivers"}], [])
    assert len(out) == 1
    rr = out[0]
    assert rr["civilization_id"] == "c1"
    assert rr["title"].startswith("Сказание о ходе")
    assert isinstance(rr["verse"], list) and rr["verse"]
    assert len(rr["sections"]) == 1
    sec = rr["sections"][0]
    assert sec["kind"] == "folklore" and sec["genre"] == "lyric"
    assert sec["text"]  # non-empty literary text shipped verbatim


def test_build_ruler_report_is_deterministic():
    view = _minimal_view()
    view["wars"] = [{"war_id": "w1", "active": 1.0,
                     "fatigue_attacker": 0.2, "fatigue_defender": 0.1}]
    report = {"years_elapsed": 25, "era_before": "classical",
              "era_after": "classical", "world_year_after": 875,
              "_civ_names": {"c1": "Rivers", "c2": "Hills"},
              "_war_parties": {"w1": {"attacker_id": "c1",
                                      "defender_id": "c2"}},
              "applied_revisions": [], "_revisions_meta": [],
              "_folklore_meta": []}
    civs = [{"id": "c1", "name": "Rivers"}]
    a = json.dumps(build_ruler_report(view, report, civs, []),
                   ensure_ascii=False, sort_keys=True)
    b = json.dumps(build_ruler_report(view, report, civs, []),
                   ensure_ascii=False, sort_keys=True)
    assert a == b  # byte-identical for identical finalized inputs (§4.1)


# ---------------------------------------------------------------------------
# engine integration: events become literature
# ---------------------------------------------------------------------------

def make_rich_engine():
    store = LawStore()
    civ_a = Civilization(id="a", name="Rivers", progress=0.25,
                         annual_production_rate=100.0)
    civ_b = Civilization(id="b", name="Hills", progress=0.25)
    store.add_law(Law(
        id="law-tax", civilization_id="a", title="Head tax",
        raw_text="Every settlement pays 1 unit per hearth.",
        canonical=CanonicalLaw(kind="tax"), complexity=1.5, tradition=0.2))
    chrono = WorldChronology("w1")
    engine = TurnEngine("w1", store, chrono, [civ_a, civ_b])
    # fatigue rate small enough that the war is still ACTIVE at turn end —
    # the ruler's epic sings of an ongoing conflict, not a concluded peace.
    engine.wars.append(War(id="w1x", world_id="w1", attacker_id="a",
                           defender_id="b", annual_fatigue_rate=0.004))
    engine.prejudices.append(Prejudice(
        id="pr1", civilization_id="a", title="Hatred of hill clans",
        target="nothing", intensity=0.40))
    engine.folklore.append(FolkloreItem(
        id="f1", civilization_id="a", title="Ballad of the ford",
        spread=0.6, mutation=0.6))
    return store, chrono, engine


def test_run_turn_wraps_events_into_ruler_reports():
    store, chrono, engine = make_rich_engine()
    rev = store.record_revision(
        law_id="law-tax", civilization_id="a",
        action=LawUserAction.AMEND_LAW,
        raw_text="Exempt small settlements.",
        canonical_change=CanonicalLawChange(
            action=LawUserAction.AMEND_LAW,
            new_canonical=CanonicalLaw(kind="tax")), now=DAY)
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY)
    report = engine.run_turn()

    by_civ = {r["civilization_id"]: r for r in report.ruler_reports}
    assert set(by_civ) >= {"a"}          # one report per active civilization
    kinds_a = {s["kind"] for s in by_civ["a"]["sections"]}
    assert "wars" in kinds_a             # epic about the ongoing conflict
    assert "unrest" in kinds_a           # warning about growing prejudice
    assert "folklore" in kinds_a         # song from the streets
    assert "breakthroughs" in kinds_a    # research/economy narrated as verse
    genres = {s["genre"] for s in by_civ["a"]["sections"]}
    assert genres <= {"epic", "lyric", "prose", "lament", "warning"}
    # the war section speaks from MY throne — names the enemy, not self
    war_sec = next(s for s in by_civ["a"]["sections"] if s["kind"] == "wars")
    assert "Hills" in war_sec["text"] or "соседи" in war_sec["text"]
    # applied law revision becomes lawcraft prose
    assert rev.id in report.applied_revisions
    assert any(s["refs"].get("revision_id") == rev.id
               for s in by_civ["a"]["sections"])
    # rival civilization gets its own report (its own epic, its own woes)
    kinds_b = {s["kind"] for s in by_civ["b"]["sections"]}
    assert "wars" in kinds_b

    # reports are shipped inside the published client view too
    assert report.view["ruler_reports"] == report.ruler_reports


def test_exhausted_war_turn_becomes_peace_and_lament():
    """A war that ends this turn: lament for the weariness + verse for peace."""
    store = LawStore()
    civ_a = Civilization(id="a", name="Rivers", progress=0.25)
    civ_b = Civilization(id="b", name="Hills", progress=0.25)
    chrono = WorldChronology("w1")
    engine = TurnEngine("w1", store, chrono, [civ_a, civ_b])
    engine.wars.append(War(id="w2x", world_id="w1", attacker_id="a",
                           defender_id="b", annual_fatigue_rate=0.05,
                           fatigue_attacker=0.98, fatigue_defender=0.98))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY)
    report = engine.run_turn()

    by_civ = {r["civilization_id"]: r for r in report.ruler_reports}
    kinds_a = {s["kind"] for s in by_civ["a"]["sections"]}
    assert "peace" in kinds_a            # "Слово о мире" — lyric on conclusion
    peace = next(s for s in by_civ["a"]["sections"] if s["kind"] == "peace")
    assert peace["refs"].get("war_id") == "w2x"


def test_ruler_reports_are_deterministic_across_engines():
    """Two identical worlds finalize to byte-identical literary reports."""
    dumps = []
    for _ in range(2):
        store, chrono, engine = make_rich_engine()
        engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY)
        report = engine.run_turn()
        dumps.append(json.dumps(report.ruler_reports, ensure_ascii=False,
                                sort_keys=True))
    assert dumps[0] == dumps[1]


# ---------------------------------------------------------------------------
# REST API publication (§13 step 22 → §16 thin client)
# ---------------------------------------------------------------------------

def test_api_ships_ruler_reports_in_run_and_report():
    srv = WorldServer("w1")
    srv.add_civilization(Civilization(id="c1", name="Rivers", progress=0.25,
                                      annual_production_rate=100.0))
    resp = srv.handle("POST", "/api/v1/laws", {
        "id": "law-tax", "civilization_id": "c1", "title": "Head tax",
        "raw_text": "Every settlement pays 1 unit per hearth.",
        "canonical": {"kind": "tax"},
    })
    assert resp["status"] == 200, resp
    resp = srv.handle("POST", "/api/v1/worlds/w1/turns/open", {
        "started_at": "2026-10-10T00:00:00+00:00",
        "simulation_cutoff_at": "2026-10-10T23:00:00+00:00",
    })
    assert resp["status"] == 200, resp
    run = srv.handle("POST", "/api/v1/worlds/w1/turns/run", {})
    assert run["status"] == 200, run
    assert len(run["ruler_reports"]) == 1
    rr = run["ruler_reports"][0]
    assert rr["civilization_id"] == "c1"
    assert rr["sections"] and rr["verse"]
    assert run["view"]["ruler_reports"] == run["ruler_reports"]

    fetched = srv.handle("GET", "/api/v1/worlds/w1/turns/report", None)
    assert fetched["status"] == 200
    assert fetched["report"]["ruler_reports"] == run["ruler_reports"]
