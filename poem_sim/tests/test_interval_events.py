"""Tests for §19.1: interval events ("события за интервал") — the turn chronicle.

Design rules verified here:
 * Long intervals are presented as a chronicle with intermediate cultural /
   folklore milestones instead of a bare time jump;
 * short turns produce NO faked milestones ("не все процессы масштабировать
   линейно");
 * key events (era change, suspension/repeal of laws, expiring treaties)
   always appear;
 * generation depends only on GAME time — identical reports submitted at
   different wall-clock hours yield identical chronicles (§20.4).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.chronology import Civilization, WorldChronology
from poem.core import (
    CanonicalLaw,
    CanonicalLawChange,
    Law,
    LawUserAction,
    SimDuration,
    TimedEffect,
    TimedEffectKind,
    Treaty,
)
from poem.laws import LawStore
from poem.turn import TurnEngine
from poem.views import interval_events, render_dashboard_text

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


def make_engine(progress: float):
    chrono = WorldChronology("w1")
    civ = Civilization(id="c1", progress=progress)
    engine = TurnEngine("w1", LawStore(), chrono, [civ])
    return engine, chrono


# ---------------------------------------------------------------------------
# milestone density thresholds (§19.1 non-linearity)
# ---------------------------------------------------------------------------

def test_short_turn_generates_no_milestones():
    """A 2-year modern turn is too short to fabricate intermediate events."""
    from poem.turn import TurnReport
    from poem.core import Era

    report = TurnReport(
        turn_id="t", turn_number=1, era_before=Era.MODERN, era_after=Era.MODERN,
        world_year_before=100.0, world_year_after=102.0,
        turn_span=SimDuration(years=2.0), years_elapsed=2.0)
    assert interval_events(report, 100.0) == []


def test_ancient_40y_turn_gets_cultural_milestone():
    """A 40-year ancient turn yields evenly spaced chronicle milestones."""
    from poem.turn import TurnReport
    from poem.core import Era

    report = TurnReport(
        turn_id="t", turn_number=1, era_before=Era.ANCIENT, era_after=Era.ANCIENT,
        world_year_before=0.0, world_year_after=40.0,
        turn_span=SimDuration(years=40.0), years_elapsed=40.0)
    events = interval_events(report, 0.0)
    kinds = [e.kind for e in events]
    assert kinds.count("CulturalMilestone") >= 1
    # all milestones lie strictly inside the interval (game time)
    for e in events:
        assert 0.0 < e.year < 40.0


# ---------------------------------------------------------------------------
# key events always appear (§19.1 "ключевые события показывать как хронику")
# ---------------------------------------------------------------------------

def test_suspension_produces_chronicle_entry():
    engine, chrono = make_engine(progress=0.25)   # classical -> 25 y/turn
    store = engine.store
    store.add_law(Law(
        id="law-x", civilization_id="c1", title="Grain tax",
        raw_text="One bushel per field.",
        canonical=CanonicalLaw(kind="tax")))
    store.record_revision(
        "law-x", "c1", LawUserAction.SUSPEND_LAW, "Suspend for one season",
        CanonicalLawChange(action=LawUserAction.SUSPEND_LAW, suspend_years=0.25),
        now=DAY.replace(hour=2))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    report = engine.run_turn()
    kinds = [e["kind"] for e in report.events]
    assert "Suspension" in kinds
    # law must NOT auto-reinstate within the same turn (§11.7 guard intact)
    from poem.core import LawStatus
    assert store.get_law("law-x").status == LawStatus.SUSPENDED


def test_treaty_expiry_produces_chronicle_entry():
    engine, chrono = make_engine(progress=0.25)   # 25 y/turn
    engine.timed_effects.append(Treaty(
        id="tr1", world_id="w1", civilization_id="c1",
        kind=TimedEffectKind.TREATY,
        remaining=SimDuration(years=10.0),          # expires this turn
        payload={"treaty_kind": "trade"}))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY)
    report = engine.run_turn()
    kinds = [e["kind"] for e in report.events]
    assert "TreatyExpired" in kinds
    assert "tr1" in report.expired_effects


def test_era_change_produces_chronicle_entry():
    """Research pushing the world into a new era must be chronicled."""
    engine, chrono = make_engine(progress=0.24)
    civ = engine.civilizations["c1"]
    civ.annual_research_rate = 500.0     # fast advance across the era border
    civ.research_to_progress = 0.01
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY)
    report = engine.run_turn()
    if report.era_after != report.era_before:
        assert any(e["kind"] == "EraChange" for e in report.events)
    else:
        # sanity: with such a rate the era must have changed
        raise AssertionError("era did not advance despite huge research rate")


# ---------------------------------------------------------------------------
# view & published report (§13 steps 19+22)
# ---------------------------------------------------------------------------

def test_dashboard_text_contains_chronicle_lines():
    engine, chrono = make_engine(progress=0.25)   # 25 y/turn -> milestones
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY)
    report = engine.run_turn()
    text = render_dashboard_text(report.view)
    assert "Хроника хода:" in text
    assert "Ход: 1" in text


def test_chronicle_is_time_of_day_invariant():
    """Identical worlds simulated at 03:00 and 18:00 produce byte-identical
    chronicles: game time drives everything, wall clock nothing (§20.4)."""
    def run_once(hour: int):
        engine, chrono = make_engine(progress=0.25)
        start = DAY.replace(hour=hour)
        engine.open_turn(started_at=start, simulation_cutoff_at=start)
        return engine.run_turn().events

    assert run_once(3) == run_once(18)
