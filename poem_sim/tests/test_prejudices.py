"""Tests for §13 step 15: prejudice updates scaled by GAME time (§20.9).

Guarantees checked here:
 * prejudice intensity grows/fades proportionally to the turn span, never
   according to real-world time of day (§20.4);
 * a 40-year ancient turn moves prejudices far more than a 1-year modern
   turn;
 * repealing the contested law marks the prejudice as resolving, after
   which it decays instead of growing;
 * the view (§13 step 19) exposes the prejudice section for clients.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.chronology import Civilization, SimDuration, WorldChronology, prejudice_update
from poem.core import (
    CanonicalLaw,
    CanonicalLawChange,
    Law,
    LawStatus,
    LawUserAction,
    Prejudice,
)
from poem.laws import LawStore
from poem.turn import TurnEngine

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# unit: prejudice_update scales linearly with game time
# ---------------------------------------------------------------------------

def test_prejudice_grows_proportionally_to_turn_span():
    p = Prejudice(id="p1", civilization_id="c1", title="Distrust of hill clans",
                  target="hill-clans", intensity=0.10, annual_growth=0.004)
    ancient = SimDuration(years=40.0)
    modern = SimDuration(years=1.0)

    q = Prejudice(id="p2", civilization_id="c1", title="same", target="t",
                  intensity=0.10, annual_growth=0.004)

    d_ancient = prejudice_update(p, ancient)
    d_modern = prejudice_update(q, modern)
    assert d_ancient == 0.004 * 40.0
    assert d_modern == 0.004 * 1.0
    assert d_ancient == 40.0 * d_modern          # pure game-time scaling
    assert p.intensity == 0.10 + d_ancient


def test_resolved_prejudice_decays_and_never_goes_negative():
    p = Prejudice(id="p1", civilization_id="c1", title="old grudge",
                  intensity=0.005, annual_decay=0.002, resolved=True)
    delta = prejudice_update(p, SimDuration(years=40.0))
    assert delta == -0.002 * 40.0
    assert p.intensity == 0.0                    # clamped at zero


def test_prejudice_update_has_no_datetime_input():
    """§20.4: the function signature cannot even see a wall clock."""
    import inspect
    sig = inspect.signature(prejudice_update)
    names = set(sig.parameters)
    assert names == {"p", "span"}
    for p in sig.parameters.values():
        ann = p.annotation
        assert ann not in (datetime, "datetime")


# ---------------------------------------------------------------------------
# integration: engine applies step 15 inside run_turn
# ---------------------------------------------------------------------------

def make_engine_with_conflict():
    store = LawStore()
    civ = Civilization(id="c1", name="Rivers", progress=0.10)  # ancient -> 40 y/turn
    law = store.add_law(Law(
        id="law-out", civilization_id="c1", title="Ban on hill clans",
        raw_text="Hill clans may not trade in the valley.",
        canonical=CanonicalLaw(kind="ban"), complexity=1.5, tradition=0.2))
    chrono = WorldChronology("w1")
    engine = TurnEngine("w1", store, chrono, [civ])
    pr = Prejudice(id="pr1", civilization_id="c1", title="Hatred of hill clans",
                   target="law-out", intensity=0.10)
    engine.prejudices.append(pr)
    return store, chrono, engine, civ, law, pr


def test_engine_grows_prejudice_by_game_time_in_turn():
    store, chrono, engine, civ, law, pr = make_engine_with_conflict()
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    report = engine.run_turn()
    # ancient era: one turn = 40 game years
    assert report.turn_span.years == 40.0
    assert report.prejudices["pr1"]["delta"] == 0.004 * 40.0
    assert pr.intensity == 0.10 + 0.004 * 40.0
    assert not pr.resolved
    # view exposes the section (§13 step 19)
    assert any(x["prejudice_id"] == "pr1" for x in report.view["prejudices"])


def test_repeal_starts_prejudice_decay_next_turn():
    store, chrono, engine, civ, law, pr = make_engine_with_conflict()
    r = store.record_revision(
        "law-out", "c1", LawUserAction.REPEAL_LAW, "Repeal the ban",
        CanonicalLawChange(action=LawUserAction.REPEAL_LAW),
        now=DAY.replace(hour=12))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    rep1 = engine.run_turn()
    assert r.id in rep1.applied_revisions
    assert store.get_law("law-out").status == LawStatus.REPEALED
    # repeal happened during this turn's law application; the prejudice is
    # marked resolved in step 15 of THIS turn, so its update already decays
    assert pr.resolved is True
    assert rep1.prejudices["pr1"]["delta"] < 0
    before = pr.intensity
    # next turn keeps decaying
    from datetime import timedelta
    engine.open_turn(started_at=DAY + timedelta(days=1),
                     simulation_cutoff_at=DAY + timedelta(days=1, hours=23))
    rep2 = engine.run_turn()
    assert pr.intensity < before
    assert rep2.prejudices["pr1"]["resolved"] == 1.0


def test_time_of_day_never_changes_prejudice_outcome():
    results = {}
    for hour in (3, 18):
        store, chrono, engine, civ, law, pr = make_engine_with_conflict()
        rev = store.record_revision(
            "law-out", "c1", LawUserAction.CLARIFY_LAW, "Clarify wording",
            CanonicalLawChange(action=LawUserAction.CLARIFY_LAW),
            now=DAY.replace(hour=hour))
        engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
        rep = engine.run_turn()
        results[hour] = (round(pr.intensity, 10), rev.id in rep.applied_revisions)
    assert results[3] == results[18]
