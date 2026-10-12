"""Rev 4 infrastructure: politics configuration + seedable world PRNG.

TASK-077a/b (design.md §22.7, §22.11; todo.md Этап 1).

Two guarantees live here:

 * ``PoliticsConfig.enabled = False`` (the default) makes every Rev-4 hook in
   the turn pipeline a no-op — the engine then behaves EXACTLY like Rev 3
   (hard backward-compatibility constraint, test §22.9-3).

 * ``WorldRng`` is the single source of randomness of the world (§22.1):
   derived from ``world.seed`` plus turn number only — never system time,
   never random.org. The same seed and the same command history replay to
   identical rolls (test §22.9-1). The seed itself is server-side and is
   never handed to clients.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Seedable deterministic PRNG (§22.1 rule 1)
# ---------------------------------------------------------------------------

class WorldRng:
    """SplitMix64-derived stream keyed by (seed, world_id, turn, purpose).

    Deterministic and stateless per draw family: every (turn, purpose) pair
    opens its own sub-stream, so adding new kinds of rolls cannot shift the
    numbers of existing ones (replay stability across code evolution).
    """

    MASK = (1 << 64) - 1

    def __init__(self, seed: int, world_id: str) -> None:
        self.seed = int(seed)          # NEVER serialized to clients (§22.8)
        self.world_id = world_id

    @staticmethod
    def _splitmix64(x: int) -> int:
        x = (x + 0x9E3779B97F4A7C15) & WorldRng.MASK
        z = x
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & WorldRng.MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & WorldRng.MASK
        z = z ^ (z >> 31)
        return z, x

    def _key(self, turn_number: int, purpose: str, index: int) -> int:
        material = f"{self.seed}:{self.world_id}:{turn_number}:{purpose}:{index}"
        digest = hashlib.sha256(material.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big", signed=False)

    def next_float(self, turn_number: int, purpose: str, index: int = 0) -> float:
        """Uniform draw in [0, 1) for one (turn, purpose, index) slot."""
        state = self._key(turn_number, purpose, index)
        value, _ = self._splitmix64(state)
        return (value & WorldRng.MASK) / float(1 << 64)

    def choice(self, options, turn_number: int, purpose: str):
        """Deterministic pick from a non-empty sequence."""
        if not options:
            raise ValueError("choice from empty sequence")
        idx = int(self.next_float(turn_number, purpose) * len(options))
        idx = min(idx, len(options) - 1)
        return options[idx]

    def snapshot_state(self, turn_number: int) -> dict:
        """Serializable RNG provenance for the turn snapshot (§22.6 step 21).

        Contains enough to recompute every roll of that turn, but is only
        ever exposed through the admin raw endpoint — the client sees none
        of it (hidden rolls, §22.1 rule 2)."""
        return {"world_id": self.world_id, "turn": turn_number,
                "stream_key_prefix": hashlib.sha256(
                    f"{self.seed}:{self.world_id}".encode()).hexdigest()[:12]}


# ---------------------------------------------------------------------------
# Politics configuration (§22.7) — config/politics.toml, default OFF
# ---------------------------------------------------------------------------

@dataclass
class FortuneParams:
    p_min: float = 0.02               # floor of adverse-event probability
    p_max: float = 0.60               # ceiling even with zero statecraft
    k_virtue: float = 0.5             # statecraft damping of p_base (§22.1)
    shock_damage_scale: float = 0.35  # production hit at severity 1.0
    damper_strength: float = 0.4      # insurance-like laws cut amplitude


@dataclass
class EliteRates:
    grievance_tax_rate: float = 0.010     # per game-year per unit tax_load
    loyalty_stability_gain: float = 0.004 # per year of calm stability
    ambition_power_gain: float = 0.002    # ambition drifts toward power
    fear_capacity_scale: float = 0.5      # suppression limit ∝ power
    cruelty_hostile_threshold: float = 0.8
    cruelty_decay_per_year: float = 0.01  # fresh cruelty fades slowly
    style_drift_per_year: float = 0.002   # elites push the ruler's style


@dataclass
class ConspiracyParams:
    threshold: float = 0.35           # pressure needed before any roll (§22.10)
    base_ignition_p: float = 0.25     # hidden roll ignites the plot
    success_mod_statecraft: float = 0.4
    fresh_cruelty_protection: float = 0.3


@dataclass
class EquilibriumParams:
    revert_rate: float = 0.01         # per game-year pull to centre (§22.3)
    centers: dict = field(default_factory=lambda: {
        "militarization": 0.15, "tax_load": 0.25, "prestige_display": 0.2})
    tau_law: float = 80.0             # risk-premium decay constant (years)
    risk_base: float = 0.30           # premium at stability_years = 0
    complexity_investment_gain: float = 0.0015  # ∝ cheap capital invested
    dao_wuwei_per_year: float = 0.003           # ву-вэй accumulation rate
    modesty_threshold_scale: float = 0.5        # × civ progress → threshold


@dataclass
class ReportDistortionParams:
    flattery_scale: float = 0.5       # narrative bias at full tyranny
    censorship_level: float = 0.6     # drop chance band for bad news
    self_deception_gain: float = 0.004  # ruler starts believing his reports


@dataclass
class PoliticsConfig:
    enabled: bool = False             # DEFAULT OFF ⇒ exact Rev-3 behaviour
    fortune: FortuneParams = field(default_factory=FortuneParams)
    elite: EliteRates = field(default_factory=EliteRates)
    conspiracy: ConspiracyParams = field(default_factory=ConspiracyParams)
    equilibrium: EquilibriumParams = field(default_factory=EquilibriumParams)
    report: ReportDistortionParams = field(default_factory=ReportDistortionParams)

    def to_json(self) -> dict:
        """Admin-safe serialization (no secrets here; seed lives elsewhere)."""
        d = asdict(self)
        return d


def parse_politics_config(raw: dict) -> PoliticsConfig:
    """Build a PoliticsConfig from a parsed TOML mapping (tolerant overlay)."""
    pol = raw.get("politics", raw)
    cfg = PoliticsConfig(enabled=bool(pol.get("enabled", False)))
    sections = {"fortune": FortuneParams, "elite": EliteRates,
                "conspiracy": ConspiracyParams, "equilibrium": EquilibriumParams,
                "report": ReportDistortionParams}
    for name, cls in sections.items():
        target = getattr(cfg, name)
        for key, value in (pol.get(name) or {}).items():
            if not hasattr(target, key):
                raise KeyError(f"unknown politics.{name}.{key}")
            current = getattr(target, key)
            if isinstance(current, dict) and isinstance(value, dict):
                merged = dict(current)
                merged.update({k: float(v) for k, v in value.items()})
                setattr(target, key, merged)
            else:
                setattr(target, key, type(current)(value)
                        if not isinstance(value, type(current)) else value)
    _validate_politics(cfg)
    return cfg


def _validate_politics(cfg: PoliticsConfig) -> None:
    f = cfg.fortune
    if not (0.0 <= f.p_min <= f.p_max <= 1.0):
        raise ValueError("fortune: require 0 ≤ p_min ≤ p_max ≤ 1")
    if f.k_virtue < 0.0:
        raise ValueError("fortune.k_virtue must be ≥ 0")
    e = cfg.equilibrium
    if e.tau_law <= 0.0 or e.revert_rate <= 0.0:
        raise ValueError("equilibrium: tau_law and revert_rate must be > 0")
    c = cfg.conspiracy
    if not (0.0 <= c.base_ignition_p <= 1.0):
        raise ValueError("conspiracy.base_ignition_p must be in [0,1]")


def load_politics_config(path: str | Path) -> PoliticsConfig:
    with open(path, "rb") as fh:
        return parse_politics_config(tomllib.load(fh))
