"""Tests for §13 steps 16–17: diplomacy (treaties) and wars in GAME time.

§11.6 — treaty durations are stored in game years, not turn counts:
a 100-year treaty expires after 3 turns at 40 y/turn but after ~100 turns
at 1 y/turn.

§13 step 17 — war fatigue accumulates proportionally to the turn span with
diminishing gains (§19.1 "not every process scales linearly"); when both
sides are exhausted the war ends and a peace treaty is concluded whose clock
again runs in game time.

§20.4 — none of this depends on real-world time of day.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.chronology import Civilization, WorldChronology
from poem.core import SimDuration, TimedEffectKind, Treaty, War
from poem.laws import LawStore
from poem.turn import TurnEngine

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


def make_engine(progress: float):
    chrono = WorldChronology("w1")
    civ_a = Civilization(id="a", progress=progress)
    civ_b = Civilization(id="b", progress=progress)
    engine = TurnEngine("w1", LawStore(), chrono, [civ_a, civ_b])
    return engine, chrono


def run_turns(engine, chrono, n: int):
    reports = []
    for i in range(n):
        start = DAY.replace(hour=3 + i % 18)   # different hours on purpose
        engine.open_turn(started_at=start, simulation_cutoff_at=start)
        reports.append(engine.run_turn())
    return reports


# ---------------------------------------------------------------------------
# §11.6 / step 16 — treaties tick by game time, remaining reported in view
# ---------------------------------------------------------------------------

def test_treaty_remaining_years_reported_in_game_time():
    """A surviving 100-year treaty appears in report.diplomacy with its
    remaining budget reduced by exactly one turn span (classical: 25 y)."""
    engine, chrono = make_engine(progress=0.25)          # classical -> 25 y/turn
    eff = Treaty(
        id="t1", world_id="w1", civilization_id="a",
        kind=TimedEffectKind.TREATY,
        remaining=SimDuration(years=100.0),
        payload={"treaty_kind": "peace"},
    )
    engine.timed_effects.append(eff)
    rep = run_turns(engine, chrono, 1)[0]
    entry = next(d for d in rep.diplomacy if d.get("treaty_id") == "t1")
    assert entry["remaining_years"] == 75.0
    assert entry["kind"] == "peace"
    # view exposes it to the client (§13 step 19)
    assert any(d.get("treaty_id") == "t1" for d in rep.view["diplomacy"])


def test_war_peace_treaty_clock_runs_in_game_time():
    """The peace treaty born from an exhausted war ticks down by spans."""
    engine, chrono = make_engine(progress=0.25)          # 25 y/turn
    w = War(id="w1x", world_id="w1", attacker_id="a", defender_id="b",
            annual_fatigue_rate=0.05)
    w.fatigue_attacker = w.fatigue_defender = 0.99       # one tick exhausts
    engine.wars.append(w)
    rep = run_turns(engine, chrono, 1)[0]
    assert rep.wars["w1x"]["active"] == 0.0
    peace = [d for d in rep.diplomacy if d.get("kind") == "peace"
             and d.get("treaty_id") != "w1x"]
    created = [e for e in engine.timed_effects
               if e.kind == TimedEffectKind.TREATY
               and e.payload.get("ended_war_id") == "w1x"]
    assert len(created) == 1
    # created THIS turn => not ticked until the next turn (§11.7 guard)
    assert created[0].remaining.years == 25.0
    rep2 = run_turns(engine, chrono, 1)[0]
    assert created[0].remaining.years == 0.0 or \
        created[0].id in rep2.expired_effects           # expired after 2nd tick


# ---------------------------------------------------------------------------
# §13 step 17 — war fatigue scales with the turn span, diminishing (§19.1)
# ---------------------------------------------------------------------------

def test_war_fatigue_scales_with_turn_span_not_turn_count():
    """Same number of turns, different eras: the ancient world (40 y/turn)
    accumulates far more war fatigue than the modern world (2 y/turn)."""
    eng_ancient, _ = make_engine(progress=0.10)          # ancient -> 40 y/turn
    eng_modern, _ = make_engine(progress=0.80)           # modern   -> 2 y/turn
    wa = War(id="wa", world_id="w1", attacker_id="a", defender_id="b")
    wm = War(id="wm", world_id="w1", attacker_id="a", defender_id="b")
    eng_ancient.wars.append(wa)
    eng_modern.wars.append(wm)
    run_turns(eng_ancient, eng_ancient.chronology, 1)
    run_turns(eng_modern, eng_modern.chronology, 1)
    # 40 game-years vs 2 game-years of war
    assert wa.fatigue_attacker > wm.fatigue_attacker * 5


def test_war_fatigue_is_diminishing_and_ends_war_with_peace():
    """Gains shrink as exhaustion grows; once BOTH sides reach >=1.0 the war
    deactivates and a peace treaty is created (§13 step 17)."""
    engine, chrono = make_engine(progress=0.25)          # 25 y/turn
    w = War(id="w2", world_id="w1", attacker_id="a", defender_id="b",
            annual_fatigue_rate=0.02)
    engine.wars.append(w)
    reps = run_turns(engine, chrono, 3)
    g1 = reps[0].wars["w2"]["fatigue_attacker"]
    g2 = reps[1].wars["w2"]["fatigue_attacker"] - g1
    assert g2 < g1                                      # diminishing gains
    assert reps[2].wars["w2"]["active"] == 0.0          # exhausted -> peace
    assert any(e.kind == TimedEffectKind.TREATY
               and e.payload.get("ended_war_id") == "w2"
               for e in engine.timed_effects)


def test_inactive_war_does_not_accumulate_fatigue():
    engine, chrono = make_engine(progress=0.25)
    w = War(id="w3", world_id="w1", attacker_id="a", defender_id="b",
            active=False)
    engine.wars.append(w)
    run_turns(engine, chrono, 2)
    assert w.fatigue_attacker == 0.0 and w.fatigue_defender == 0.0


# ---------------------------------------------------------------------------
# §20.4 — time of day never enters the war/diplomacy clocks
# ---------------------------------------------------------------------------

def test_war_progress_identical_at_0300_and_1800():
    results = {}
    for hour in (3, 18):
        engine, chrono = make_engine(progress=0.25)
        w = War(id=f"w@{hour}", world_id="w1", attacker_id="a", defender_id="b")
        engine.wars.append(w)
        start = DAY.replace(hour=hour)
        engine.open_turn(started_at=start, simulation_cutoff_at=start)
        rep = engine.run_turn()
        results[hour] = dict(rep.wars[w.id])
    assert results[3] == results[18]
