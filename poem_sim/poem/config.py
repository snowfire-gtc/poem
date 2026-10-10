"""TOML configuration loading & validation for chronology (§8, TASK-065).

Allows changing intervals/speeds without touching code:

    config = load_chronology_config("config/chronology.toml")
    chronology = build_chronology(config, world_id="w1")
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .chronology import (
    ChronologyConfigError,
    TurnIntervalRule,
    WorldChronology,
    validate_chronology_config,
)
from .core import Era, EraSource, GameSpeed, SimDuration


@dataclass
class ChronologyConfig:
    mode: str = "civilization_like"
    speed: GameSpeed = GameSpeed.STANDARD
    era_source: EraSource = EraSource.MEDIAN_ACTIVE_CIVILIZATIONS
    year_length_days: float = 365.25
    seasons_per_year: int = 4
    intervals: dict[Era, float] = field(default_factory=dict)
    speed_multipliers: dict[GameSpeed, float] = field(default_factory=dict)


def load_chronology_config(path: str | Path) -> ChronologyConfig:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    return parse_chronology_config(raw)


def parse_chronology_config(raw: dict) -> ChronologyConfig:
    ch = raw.get("chronology", raw)
    mode = ch.get("mode", "civilization_like")
    if mode != "civilization_like":
        raise ChronologyConfigError(f"unsupported chronology mode '{mode}'")

    cfg = ChronologyConfig(
        mode=mode,
        speed=GameSpeed(ch.get("speed", "standard")),
        era_source=EraSource(ch.get("era_source", "median_active_civilizations")),
    )

    cal = ch.get("calendar", {})
    cfg.year_length_days = float(cal.get("year_length_days", 365.25))
    cfg.seasons_per_year = int(cal.get("seasons_per_year", 4))

    for entry in ch.get("intervals", []):
        cfg.intervals[Era(entry["era"])] = float(entry["years_per_turn"])

    speeds = ch.get("speeds", {})
    for name, mult in speeds.items():
        key = name.removesuffix("_multiplier")
        cfg.speed_multipliers[GameSpeed(key)] = float(mult)

    validate_chronology_config(cfg.intervals, {k.value: v for k, v in cfg.speed_multipliers.items()},
                               {"year_length_days": cfg.year_length_days,
                                "seasons_per_year": cfg.seasons_per_year})
    return cfg


def build_chronology(cfg: ChronologyConfig, world_id: str) -> WorldChronology:
    """Instantiate a WorldChronology from validated configuration."""
    yld = cfg.year_length_days
    spy = cfg.seasons_per_year
    table = [
        TurnIntervalRule(
            era,
            SimDuration(years=y, seasons=y * spy, days=y * yld),
        )
        for era, y in sorted(cfg.intervals.items(), key=lambda kv: kv[0].rank)
    ]
    multipliers = dict(cfg.speed_multipliers) if cfg.speed_multipliers else None
    chrono = WorldChronology(
        world_id=world_id,
        game_speed=cfg.speed,
        era_source=cfg.era_source,
        interval_table=table,
        year_length_days=yld,
        seasons_per_year=spy,
    )
    if multipliers:
        chrono.speed_multipliers = multipliers
    # Ensure every configured era has a rule and current era is covered.
    for era in Era:
        try:
            chrono.rule_for(era)
        except ChronologyConfigError:
            raise ChronologyConfigError(f"interval table missing era '{era.value}'") from None
    chrono.current_era = chrono.rule_for(chrono.current_era).era
    return chrono
