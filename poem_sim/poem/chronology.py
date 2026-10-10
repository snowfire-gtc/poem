"""World chronology with Civilization-like dynamic turn intervals.

TASK-063 / TASK-064 / TASK-065 (§7–§10).

Real server schedule and in-game time are strictly separated (§6.1):
 * The server may run "one turn per day" — that is a technical cycle only.
 * In-game, one turn advances the world by ``turn_span`` years, where the
   span depends on the current world era and the configured game speed.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .core import (
    Era,
    EraSource,
    GameSpeed,
    SeasonId,
    SimDuration,
    apply_speed_multiplier,
)


# ---------------------------------------------------------------------------
# Interval table (§7.2, §8.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TurnIntervalRule:
    era: Era
    time_per_turn: SimDuration


def default_interval_table() -> list[TurnIntervalRule]:
    """Fallback table matching the TOML example of §8.1 (standard speed)."""
    years = {
        Era.DAWN: 60.0,
        Era.ANCIENT: 40.0,
        Era.CLASSICAL: 25.0,
        Era.MEDIEVAL: 20.0,
        Era.RENAISSANCE: 10.0,
        Era.INDUSTRIAL: 5.0,
        Era.MODERN: 2.0,
        Era.ATOMIC: 1.0,
        Era.INFORMATION: 0.5,
        Era.PLANETARY: 0.25,
    }
    return [
        TurnIntervalRule(era, SimDuration(years=y, seasons=4.0 * y, days=365.25 * y))
        for era, y in years.items()
    ]


DEFAULT_SPEED_MULTIPLIERS: dict[GameSpeed, float] = {
    GameSpeed.QUICK: 0.67,
    GameSpeed.STANDARD: 1.0,
    GameSpeed.EPIC: 1.5,
    GameSpeed.MARATHON: 3.0,
}


class ChronologyConfigError(ValueError):
    pass


def validate_chronology_config(
    intervals: dict[Era, float],
    speeds: dict[str, float],
    calendar: dict,
) -> None:
    """TASK-065: configuration validation."""
    if not intervals:
        raise ChronologyConfigError("interval table is empty")
    for era, ypt in intervals.items():
        if ypt <= 0:
            raise ChronologyConfigError(f"years_per_turn for {era.value} must be > 0")
    # Intervals should shrink (or stay equal) as eras advance — otherwise the
    # Civilization-like premise is inverted.
    ordered = sorted(intervals.items(), key=lambda kv: kv[0].rank)
    for (e1, v1), (e2, v2) in zip(ordered, ordered[1:]):
        if v2 > v1:
            raise ChronologyConfigError(
                f"{e2.value}.years_per_turn ({v2}) > {e1.value} ({v1}): "
                "later eras must not have longer turns"
            )
    for name, m in speeds.items():
        if m <= 0:
            raise ChronologyConfigError(f"speed multiplier '{name}' must be > 0")
    yld = calendar.get("year_length_days", 365.25)
    spy = calendar.get("seasons_per_year", 4)
    if yld <= 0 or spy <= 0 or float(yld) < float(spy):
        raise ChronologyConfigError("invalid calendar parameters")


# ---------------------------------------------------------------------------
# Civilizations & era determination (§9)
# ---------------------------------------------------------------------------

@dataclass
class Civilization:
    id: str
    name: str = ""
    progress: float = 0.0          # 0..1 development index used for era mapping
    active: bool = True
    population: float = 1000.0
    annual_production_rate: float = 0.0
    annual_growth_rate: float = 0.005
    annual_research_rate: float = 0.0
    culture_modifier: float = 1.0
    technology_modifier: float = 1.0


PROGRESS_ERA_THRESHOLDS: list[tuple[float, Era]] = [
    (0.00, Era.DAWN),
    (0.08, Era.ANCIENT),
    (0.20, Era.CLASSICAL),
    (0.35, Era.MEDIEVAL),
    (0.50, Era.RENAISSANCE),
    (0.65, Era.INDUSTRIAL),
    (0.78, Era.MODERN),
    (0.86, Era.ATOMIC),
    (0.93, Era.INFORMATION),
    (0.98, Era.PLANETARY),
]


def era_for_progress(progress: float) -> Era:
    era = PROGRESS_ERA_THRESHOLDS[0][1]
    for threshold, candidate in PROGRESS_ERA_THRESHOLDS:
        if progress >= threshold:
            era = candidate
    return era


def determine_world_era(
    chronology: "WorldChronology",
    civilizations: list[Civilization],
) -> Era:
    """§9: configurable era source; default = median of active civs."""
    active = [c for c in civilizations if c.active]
    if not active:
        return chronology.current_era
    match chronology.era_source:
        case EraSource.LEADING_CIVILIZATION:
            best = max(active, key=lambda c: c.progress)
            return era_for_progress(best.progress)
        case EraSource.WEIGHTED_AVERAGE:
            total_pop = sum(c.population for c in active) or 1.0
            weighted = sum(c.progress * c.population for c in active) / total_pop
            return era_for_progress(weighted)
        case EraSource.MEDIAN_ACTIVE_CIVILIZATIONS:
            med = statistics.median(c.progress for c in active)
            return era_for_progress(med)
    raise ChronologyConfigError(f"unknown era source {chronology.era_source}")


# ---------------------------------------------------------------------------
# WorldChronology (§7.1)
# ---------------------------------------------------------------------------

_SEASON_CYCLE: tuple[SeasonId, ...] = (
    SeasonId.SPRING,
    SeasonId.SUMMER,
    SeasonId.AUTUMN,
    SeasonId.WINTER,
)


def advance_season(season: SeasonId, delta_seasons: float, cycle: int = 4) -> SeasonId:
    try:
        idx = _SEASON_CYCLE.index(season)
    except ValueError:
        idx = 0  # setting-specific seasons (e.g. LONG_NIGHT) map into the cycle
    new_idx = (idx + int(delta_seasons)) % min(cycle, len(_SEASON_CYCLE))
    return _SEASON_CYCLE[new_idx]


@dataclass
class WorldChronology:
    world_id: str
    game_speed: GameSpeed = GameSpeed.STANDARD
    era_source: EraSource = EraSource.MEDIAN_ACTIVE_CIVILIZATIONS
    current_turn: int = 0
    current_year: float = 0.0
    current_season: SeasonId = SeasonId.SPRING
    current_era: Era = Era.DAWN
    interval_table: list[TurnIntervalRule] = field(default_factory=default_interval_table)
    speed_multipliers: dict[GameSpeed, float] = field(
        default_factory=lambda: dict(DEFAULT_SPEED_MULTIPLIERS)
    )
    year_length_days: float = 365.25
    seasons_per_year: int = 4

    def rule_for(self, era: Era) -> TurnIntervalRule:
        for r in self.interval_table:
            if r.era == era:
                return r
        raise ChronologyConfigError(f"no interval rule for era {era.value}")

    @property
    def speed_multiplier(self) -> float:
        return self.speed_multipliers[self.game_speed]


# ---------------------------------------------------------------------------
# Turn span calculation & chronology advance (§10)
# ---------------------------------------------------------------------------

def calculate_turn_span(
    chronology: WorldChronology,
    civilizations: list[Civilization],
) -> SimDuration:
    """§10.1: era -> base interval -> speed multiplier."""
    era = determine_world_era(chronology, civilizations)
    rule = chronology.rule_for(era)
    return apply_speed_multiplier(rule.time_per_turn, chronology.speed_multiplier)


def advance_chronology(
    chronology: WorldChronology,
    span: SimDuration,
    civilizations: list[Civilization],
) -> tuple[Era, Era]:
    """§10.2: push world date forward by one turn interval.

    Returns (era_before, era_after).
    """
    era_before = chronology.current_era
    chronology.current_year += span.years
    chronology.current_season = advance_season(
        chronology.current_season,
        span.seasons if span.seasons else span.years * chronology.seasons_per_year,
        cycle=chronology.seasons_per_year,
    )
    chronology.current_turn += 1
    chronology.current_era = determine_world_era(chronology, civilizations)
    return era_before, chronology.current_era


# ---------------------------------------------------------------------------
# Time-scaled system formulas (§11)
# ---------------------------------------------------------------------------

def production_this_turn(annual_rate: float, span: SimDuration) -> float:
    """§11.1"""
    return annual_rate * span.years


def population_growth_this_turn(
    population: float, annual_growth_rate: float, span: SimDuration,
    food_cap: float | None = None, stability: float = 1.0,
) -> float:
    """§11.2 with simple caps."""
    growth = population * annual_growth_rate * span.years * stability
    if food_cap is not None:
        growth = min(growth, max(food_cap - population, 0.0))
    return max(growth, 0.0)


def research_progress_this_turn(
    annual_research_rate: float, span: SimDuration,
    culture_modifier: float = 1.0, technology_modifier: float = 1.0,
) -> float:
    """§11.3"""
    return annual_research_rate * span.years * culture_modifier * technology_modifier


def tradition_gain(annual_tradition_gain: float, span: SimDuration, cultural_resonance: float = 1.0) -> float:
    """§11.4 — laws accumulate tradition proportionally to GAME time."""
    return annual_tradition_gain * span.years * cultural_resonance


def folklore_spread_gain(
    annual_spread_rate: float, span: SimDuration,
    carrier_strength: float = 1.0, oral_tradition_modifier: float = 1.0,
) -> float:
    """§11.5"""
    return annual_spread_rate * span.years * carrier_strength * oral_tradition_modifier


def folklore_mutation_gain(
    annual_mutation_rate: float, span: SimDuration,
    oral_strength: float = 1.0, distance_factor: float = 1.0, conflict_factor: float = 1.0,
) -> float:
    """§11.5"""
    return annual_mutation_rate * span.years * oral_strength * distance_factor * conflict_factor
