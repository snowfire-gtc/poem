"""Rev 4 §22.1 — Fortune: hidden deterministic rolls (Machiavelli, ch. XXV).

TASK-070. «Fortune rules half of human affairs; the other half is left to
the art of the ruler.» Implemented as:

 * every adverse event of a turn is one draw from ``WorldRng`` keyed by
   (world seed, turn number) — fully reproducible, never system time;
 * the ruler's accumulated ``statecraft`` damps the probability:
   ``p_eff = clamp(p_base · (1 − k·statecraft), p_min, p_max)``;
 * damper laws (insurance / granary / commons kinds) reduce the AMPLITUDE
   of an event that already struck, not its chance — "the wise ruler stores
   lightning in advance";
 * raw roll values are written into the server-side snapshot only. The
   client receives consequences (chronicle events), never numbers (§22.1-2,
   test §22.9-2).
"""

from __future__ import annotations

from dataclasses import dataclass

from .core import FortuneRoll


@dataclass(frozen=True)
class FortuneEventSpec:
    """One row of the Fortune event table (§22.1-3)."""

    event_id: str
    title: str
    p_base: float          # base annual-scale probability for the interval
    severity: float        # amplitude 0..1 before dampers
    harm: str              # "production" | "population" | "folklore" | "unrest"
    detail: str = ""


#: The canonical event table. Deterministic order — iteration over this
#: tuple defines the roll slot index, so it must never be reordered lightly.
FORTUNE_EVENTS: tuple[FortuneEventSpec, ...] = (
    FortuneEventSpec("poor_harvest", "Неурожай", 0.18, 0.5, "production",
                     "Поля дали меньше обычного; казна недосчиталась хлеба."),
    FortuneEventSpec("flood", "Наводнение", 0.12, 0.6, "production",
                     "Река вышла из берегов и затопила низинные поля."),
    FortuneEventSpec("epidemic", "Эпидемия", 0.08, 0.7, "population",
                     "Болезнь прошлась по городам; ремесло встало."),
    FortuneEventSpec("folk_uproar", "Народное волнение", 0.10, 0.4, "unrest",
                     "Слухи о тяготах вызвали ропот на площадях."),
    FortuneEventSpec("bard_outbreak", "Вспышка фольклора", 0.15, 0.3, "folklore",
                     "Певцы разносят новую историю быстрее дорог."),
    FortuneEventSpec("quake", "Землетрясение", 0.05, 0.8, "production",
                     "Земля дрожала; стены рушились, рынки опустели."),
)

#: Law kinds that act as shock absorbers (§22.1-5): they damp amplitude.
DAMPER_KINDS = {"insurance", "granary", "commons", "welfare", "relief"}


def effective_probability(p_base: float, statecraft: float,
                          p_min: float, p_max: float, k_virtue: float) -> float:
    """§22.1-4 virtue modifier: Fortune is tamed, never cancelled."""
    p_eff = p_base * (1.0 - k_virtue * max(0.0, min(statecraft, 1.0)))
    return max(p_min, min(p_max, p_eff))


def damper_factor(law_kinds: set[str], damper_strength: float) -> float:
    """Amplitude multiplier in (0,1]: each distinct damper law cuts the hit,
    with diminishing returns (two granaries help less than twice one)."""
    n = len(law_kinds & DAMPER_KINDS)
    if n <= 0:
        return 1.0
    return 1.0 / (1.0 + damper_strength * n)


def roll_fortune(rng, turn_number: int, civ_id: str, statecraft: float,
                 law_kinds: set[str], params) -> tuple[list[FortuneRoll], dict]:
    """Step 13a: draw every Fortune event for one civilization this turn.

    Returns (rolls, impacts) where impacts aggregates damage per harm class
    AFTER dampers, ready for step 8a economy adjustments. Rolls contain the
    hidden raw numbers and must go to the server snapshot only.
    """
    rolls: list[FortuneRoll] = []
    impacts: dict[str, float] = {}
    damp = damper_factor(law_kinds, params.damper_strength)
    for idx, spec in enumerate(FORTUNE_EVENTS):
        threshold = effective_probability(
            spec.p_base, statecraft, params.p_min, params.p_max, params.k_virtue)
        value = rng.next_float(turn_number, f"fortune:{civ_id}", idx)
        struck = value < threshold
        roll = FortuneRoll(
            event_id=spec.event_id, title=spec.title, severity=spec.severity,
            roll_value=value, threshold=threshold, success=struck,
            civ_id=civ_id, detail=spec.detail)
        rolls.append(roll)
        if struck:
            impact = spec.severity * damp * params.shock_damage_scale
            impacts[spec.harm] = impacts.get(spec.harm, 0.0) + impact
    return rolls, impacts
