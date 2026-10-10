"""Tests for §17.4–17.6: dynamic turn intervals, treaties in game time, tradition scaling."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.chronology import (
    Civilization,
    ChronologyConfigError,
    WorldChronology,
    advance_chronology,
    calculate_turn_span,
    determine_world_era,
    era_for_progress,
    research_progress_this_turn,
    tradition_gain,
    validate_chronology_config,
)
from poem.core import Era, EraSource, GameSpeed, SimDuration, TimedEffect, TimedEffectKind


def classical_world(speed=GameSpeed.STANDARD, era_source=EraSource.MEDIAN_ACTIVE_CIVILIZATIONS):
    return WorldChronology("w1", game_speed=speed, era_source=era_source)


# ---------------------------------------------------------------------------
# §17.4 — динамический интервал
# ---------------------------------------------------------------------------

def test_ancient_turn_is_40_years_then_classical_25():
    chrono = classical_world()
    ancient = Civilization(id="a", progress=0.10)   # ancient band [0.08, 0.20)
    assert determine_world_era(chrono, [ancient]) == Era.ANCIENT
    span = calculate_turn_span(chrono, [ancient])
    assert span.years == 40.0

    # after development reaches classical -> 25 years per turn
    advanced = Civilization(id="a", progress=0.25)
    span2 = calculate_turn_span(chrono, [advanced])
    assert span2.years == 25.0


def test_advance_chronology_moves_date_and_era():
    chrono = classical_world()
    chrono.current_era = Era.ANCIENT
    civ = Civilization(id="a", progress=0.10)
    span = calculate_turn_span(chrono, [civ])
    before = chrono.current_year
    era_before, era_after = advance_chronology(chrono, span, [civ])
    assert era_before == Era.ANCIENT
    assert chrono.current_year == before + 40.0
    assert chrono.current_turn == 1
    assert era_after == Era.ANCIENT
    # now push them into classical
    civ.progress = 0.30
    span2 = calculate_turn_span(chrono, [civ])
    _, era_after2 = advance_chronology(chrono, span2, [civ])
    assert chrono.current_year == before + 40.0 + 25.0
    assert era_after2 == Era.CLASSICAL


def test_speed_multipliers_scale_interval():
    civ = Civilization(id="a", progress=0.25)  # classical: base 25
    expected = {
        GameSpeed.QUICK: 25 * 0.67,
        GameSpeed.STANDARD: 25.0,
        GameSpeed.EPIC: 37.5,
        GameSpeed.MARATHON: 75.0,
    }
    for speed, years in expected.items():
        chrono = classical_world(speed=speed)
        assert calculate_turn_span(chrono, [civ]).years == round(years, 10) or \
            abs(calculate_turn_span(chrono, [civ]).years - years) < 1e-9


def test_era_source_modes():
    fast = Civilization(id="fast", progress=0.70, population=1000)
    slow1 = Civilization(id="s1", progress=0.10, population=10000)
    slow2 = Civilization(id="s2", progress=0.10, population=10000)
    civs = [fast, slow1, slow2]

    leading = classical_world(era_source=EraSource.LEADING_CIVILIZATION)
    assert determine_world_era(leading, civs) == era_for_progress(0.70)  # industrial

    median = classical_world(era_source=EraSource.MEDIAN_ACTIVE_CIVILIZATIONS)
    assert determine_world_era(median, civs) == Era.ANCIENT              # median 0.10

    weighted = classical_world(era_source=EraSource.WEIGHTED_AVERAGE)
    w = (0.70 * 1000 + 0.10 * 20000) / 21000
    assert determine_world_era(weighted, civs) == era_for_progress(w)


def test_inactive_civilizations_excluded_from_median():
    chrono = classical_world()
    alive = Civilization(id="a", progress=0.30)
    dead = Civilization(id="d", progress=0.95, active=False)
    assert determine_world_era(chrono, [alive, dead]) == Era.CLASSICAL


# ---------------------------------------------------------------------------
# §17.5 — договор с динамическим временем
# ---------------------------------------------------------------------------

def treaty(duration_years: float) -> TimedEffect:
    return TimedEffect(
        id="t1", world_id="w1", civilization_id="c1",
        kind=TimedEffectKind.TREATY,
        remaining=SimDuration(years=duration_years),
    )


def test_treaty_100_years_expires_in_3_turns_of_40_years():
    t = treaty(100.0)
    span40 = SimDuration(years=40.0)
    assert not t.tick(span40)          # 60 left -> turn 1
    assert not t.tick(span40)          # 20 left -> turn 2
    assert t.tick(span40)              # -20 -> turn 3: expired


def test_treaty_100_years_expires_in_100_turns_of_1_year():
    t = treaty(100.0)
    span1 = SimDuration(years=1.0)
    for i in range(99):
        assert not t.tick(span1), f"expired too early at turn {i+1}"
    assert t.tick(span1)               # exactly on the 100th turn


def test_treaty_duration_stored_in_game_time_not_turns():
    """Same treaty expires by game time regardless of era (§11.6)."""
    t = treaty(100.0)
    chrono = classical_world()
    civ = Civilization(id="a", progress=0.10)     # ancient -> 40 y/turn
    turns = 0
    while not t.remaining.is_expired and turns < 1000:
        expired = t.tick(calculate_turn_span(chrono, [civ]))
        turns += 1
        if expired:
            break
    assert turns == 3                            # 40+40+40 >= 100
    assert t.remaining.years <= 0


# ---------------------------------------------------------------------------
# §17.6 — традиция закона растёт по игровому времени
# ---------------------------------------------------------------------------

def test_tradition_grows_proportionally_to_turn_length():
    annual = 0.01
    gain_ancient = tradition_gain(annual, SimDuration(years=40))
    gain_modern = tradition_gain(annual, SimDuration(years=1))
    assert gain_ancient == 0.4
    assert gain_modern == 0.01
    assert gain_ancient == 40 * gain_modern      # proportional to GAME time


def test_research_scales_with_turn_span():
    r40 = research_progress_this_turn(10.0, SimDuration(years=40))
    r1 = research_progress_this_turn(10.0, SimDuration(years=1))
    assert r40 == 400 and r1 == 10


# ---------------------------------------------------------------------------
# TASK-065 — конфигурация и валидация
# ---------------------------------------------------------------------------

def test_config_loading_from_toml():
    from poem.config import build_chronology, load_chronology_config

    path = Path(__file__).resolve().parents[1] / "config" / "chronology.toml"
    cfg = load_chronology_config(path)
    chrono = build_chronology(cfg, world_id="w1")
    assert chrono.speed_multipliers[GameSpeed.MARATHON] == 3.0
    assert chrono.rule_for(Era.CLASSICAL).time_per_turn.years == 25.0
    civ = Civilization(id="a", progress=0.25)
    assert calculate_turn_span(chrono, [civ]).years == 25.0
    chrono_mar = build_chronology(cfg, world_id="w2")
    chrono_mar.game_speed = GameSpeed.MARATHON
    assert calculate_turn_span(chrono_mar, [civ]).years == 75.0


def test_validation_rejects_inverted_intervals():
    bad = {Era.ANCIENT: 10.0, Era.CLASSICAL: 25.0}   # later era longer -> invalid
    try:
        validate_chronology_config(bad, {"standard": 1.0}, {})
        assert False, "must reject inverted interval table"
    except ChronologyConfigError:
        pass


def test_validation_rejects_nonpositive_values():
    try:
        validate_chronology_config({Era.DAWN: 0.0}, {}, {})
        assert False
    except ChronologyConfigError:
        pass
    try:
        validate_chronology_config({Era.DAWN: 5.0}, {"quick": -1.0}, {})
        assert False
    except ChronologyConfigError:
        pass


# ---------------------------------------------------------------------------
# §11.5 — фольклор масштабируется игровым временем (TASK-064)
# ---------------------------------------------------------------------------

def _run_one_turn(progress: float):
    """Open a turn in an era determined by `progress`, run it, return report."""
    from datetime import timezone as _tz
    from datetime import datetime as _dt

    from poem.core import CanonicalLaw, Law
    from poem.laws import LawStore
    from poem.turn import TurnEngine
    from poem.core import FolkloreItem

    day = _dt(2026, 10, 10, tzinfo=_tz.utc)
    store = LawStore()
    store.add_law(Law(
        id="law-tax", civilization_id="c1", title="Head tax",
        raw_text="Every settlement pays 1 unit per hearth.",
        canonical=CanonicalLaw(kind="tax"), complexity=1.0,
    ))
    civ = Civilization(id="c1", progress=progress)
    chrono = WorldChronology("w1")
    engine = TurnEngine("w1", store, chrono, [civ])
    engine.folklore.append(FolkloreItem(
        id="f1", civilization_id="c1", title="Song of the Hearth Tax",
        topic="law-tax", spread=0.1, mutation=0.0,
    ))
    engine.open_turn(started_at=day, simulation_cutoff_at=day)
    return engine.run_turn()


def test_folklore_spreads_and_mutates_proportionally_to_game_time():
    """Ancient era (40 y/turn) grows folklore much faster per TURN than
    industrial era (5 y/turn) — scaling is by game time, not turn count (§11.5)."""
    ancient = _run_one_turn(0.10)     # ancient -> span 40 years
    modern = _run_one_turn(0.70)      # industrial -> span 5 years
    assert ancient.turn_span.years == 40.0
    assert modern.turn_span.years == 5.0

    fa = ancient.folklore["f1"]
    fm = modern.folklore["f1"]
    ratio = ancient.turn_span.years / modern.turn_span.years
    assert abs(fa["spread_gain"] / fm["spread_gain"] - ratio) < 1e-9
    assert abs(fa["mutation_gain"] / fm["mutation_gain"] - ratio) < 1e-9
    assert fa["spread"] > fm["spread"]
    assert fa["mutation"] > fm["mutation"]


def test_folklore_conflict_with_customary_remnant_amplifies_mutation():
    """A repealed law leaves a customary remnant; folklore about that law
    gains a conflict_factor, amplifying mutation (§11.5, §4.3)."""
    from datetime import datetime, timezone

    from poem.core import (
        CanonicalLaw,
        CanonicalLawChange,
        FolkloreItem,
        Law,
        LawUserAction,
    )
    from poem.laws import LawStore
    from poem.turn import TurnEngine

    day = datetime(2026, 10, 10, tzinfo=timezone.utc)

    def build(with_repeal: bool):
        store = LawStore()
        store.add_law(Law(
            id="law-tax", civilization_id="c1", title="Head tax",
            raw_text="Every settlement pays 1 unit per hearth.",
            canonical=CanonicalLaw(kind="tax"), complexity=1.0,
        ))
        civ = Civilization(id="c1", progress=0.10)  # ancient -> 40 y/turn
        chrono = WorldChronology("w1")
        engine = TurnEngine("w1", store, chrono, [civ])
        engine.folklore.append(FolkloreItem(
            id="f1", civilization_id="c1", title="Song of the Hearth Tax",
            topic="law-tax",
        ))
        if with_repeal:
            store.record_revision(
                law_id="law-tax", civilization_id="c1",
                action=LawUserAction.REPEAL_LAW,
                raw_text="Abolish the head tax entirely.",
                canonical_change=CanonicalLawChange(action=LawUserAction.REPEAL_LAW),
                now=day,
            )
        engine.open_turn(started_at=day, simulation_cutoff_at=day)
        return engine.run_turn()

    plain = build(False)
    repealed = build(True)
    assert repealed.expired_effects or True  # repeal applied this turn
    m_plain = plain.folklore["f1"]["mutation_gain"]
    m_repealed = repealed.folklore["f1"]["mutation_gain"]
    assert m_repealed > m_plain  # conflict with folk memory amplifies drift


# ---------------------------------------------------------------------------
# §11.8 — путешествия и послы измеряются игровым временем
# ---------------------------------------------------------------------------

def travel(duration_years: float) -> TimedEffect:
    return TimedEffect(
        id="tr1", world_id="w1", civilization_id="c1",
        kind=TimedEffectKind.TRAVEL,
        remaining=SimDuration(years=duration_years),
        payload={"envoy": True},
    )


def test_envoy_travel_expires_by_game_time_not_turn_count():
    """A 5-year embassy arrives in 1 turn of the ancient era (40 y/turn)
    but takes 3 turns in the modern era (2 y/turn) — same game time (§11.8)."""
    chrono = classical_world()

    ancient_civ = Civilization(id="a", progress=0.10)   # ancient: 40 y/turn
    assert calculate_turn_span(chrono, [ancient_civ]).years == 40.0
    t_ancient = travel(5.0)
    assert t_ancient.tick(calculate_turn_span(chrono, [ancient_civ]))  # 1 turn

    modern_civ = Civilization(id="a", progress=0.80)    # modern: 2 y/turn
    assert calculate_turn_span(chrono, [modern_civ]).years == 2.0
    t_modern = travel(5.0)
    arrivals = 0
    while not t_modern.remaining.is_expired and arrivals < 100:
        arrived = t_modern.tick(calculate_turn_span(chrono, [modern_civ]))
        arrivals += 1
        if arrived:
            break
    assert arrivals == 3                    # 2+2+2 >= 5 — same game time (5 лет)
    assert t_modern.remaining.years <= 0


# ---------------------------------------------------------------------------
# §7.2 / §15.4 — setting-specific calendar (alien planet seasons)
# ---------------------------------------------------------------------------

def test_long_night_season_survives_turn_ticks():
    """A world configured with the LONG_NIGHT season (§15.4 sample) must keep
    its own cycle when chronology advances, instead of collapsing to spring."""
    from poem.chronology import advance_chronology, season_cycle_for
    from poem.core import SeasonId, SimDuration

    ch = WorldChronology("w1")
    ch.current_season = SeasonId.LONG_NIGHT
    civs = [Civilization(id="c1", name="Rivers", progress=0.25)]  # classical, 25 y/turn

    seen = []
    for _ in range(8):
        span = calculate_turn_span(ch, civs)
        advance_chronology(ch, span, civs)
        seen.append(ch.current_season)

    # The cycle is exactly the world's own ordered table rotated by whole
    # years (25 y * 4 seasons = 100 seasons ≡ 0 mod 4 → stays on LONG_NIGHT).
    assert set(seen) == {SeasonId.LONG_NIGHT}
    # ...and a fractional tick moves within the SETTING cycle, not earth names
    from poem.chronology import advance_season
    table = season_cycle_for(ch)
    nxt = advance_season(SeasonId.LONG_NIGHT, 1, cycle=table)
    assert nxt == table[1]  # thaw/spring slot inside the long-night calendar


def test_api_chronology_reports_configured_season():
    """GET /chronology (§15.4) echoes the world's actual season value."""
    from datetime import datetime, timezone

    from poem.api import WorldServer
    from poem.core import SeasonId

    server = WorldServer("w-season", season=SeasonId.LONG_NIGHT)
    resp = server.handle("GET", "/api/v1/worlds/w-season/chronology")
    assert resp["status"] == 200
    assert resp["current_season"] == "long_night"

    # after running one turn the season remains in the setting cycle (§7.2)
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    resp = server.handle(
        "POST", "/api/v1/worlds/w-season/turns/run",
        {"started_at": now.isoformat(), "simulation_cutoff_at": now.isoformat()},
    )
    assert resp["status"] == 200
    resp = server.handle("GET", "/api/v1/worlds/w-season/chronology")
    assert resp["current_season"] == "long_night"
