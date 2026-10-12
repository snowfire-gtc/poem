"""Rev 4 §22.2 — elites, loyalty dynamics, conspiracies, succession.

TASK-071 / TASK-072. The ruler does not govern a civilization directly — he
governs the elites, and they govern him (and against him).

Key mechanics implemented here (all in GAME time, all deterministic):

 * factions form out of the economy profile (§22.2.1);
 * grievance grows with taxation/expropriation/ideological humiliation,
   loyalty feeds on stability; fear suppresses grievance only up to a
   capacity that scales with faction power — «a people stripped of
   everything loses even fear» (§22.2.2);
 * repeated cruelty inside one window multiplies into the
   ``cruelty_accumulator``; past the threshold factions turn irreversibly
   hostile;
 * conspiracy pressure = Σ power·max(0, −loyalty − threshold) (§22.2.3);
   when it clears the configured bar, ONE hidden Fortune roll decides
   ignition, another decides its outcome;
 * SUCCESSION IS MECHANICAL: the winner is chosen by faction weights
   ``power × circumstance-roll``; the player has NO API to pick an heir
   (design decision #2) — civilization and laws survive, the ruler changes.
"""

from __future__ import annotations

import uuid

from .core import (
    ConspiracyEvent,
    ConspiracyOutcome,
    EliteFaction,
    Ideology,
    RulerState,
)

#: Default economic structure → elite shares (§22.2.1). A civ without an
#: explicit profile gets this baseline so faction formation is always
#: defined once politics is enabled.
DEFAULT_ECONOMY_PROFILE: dict[str, float] = {
    "agriculture": 0.45,
    "trade": 0.20,
    "military_production": 0.12,
    "religion": 0.08,
    "administration": 0.08,
    "craft": 0.07,
}

#: sector share → elite order that lives off it
SECTOR_TO_ORDER: dict[str, str] = {
    "religion": "clergy",
    "land_aristocracy": "nobility",
    "agriculture": "nobility",
    "trade": "merchants",
    "military_production": "military",
    "administration": "scribes",
    "writing": "scribes",
    "craft": "artisans",
}

ORDER_IDEOLOGY: dict[str, Ideology] = {
    "clergy": Ideology.TRADITIONALIST,
    "nobility": Ideology.TRADITIONALIST,
    "merchants": Ideology.LIBERAL,
    "military": Ideology.AUTHORITARIAN,
    "scribes": Ideology.LIBERAL,
    "artisans": Ideology.LIBERAL,
}

#: law kinds read as repressive / liberal by the style-drift mechanic
#: (§22.2.4: bans, censorship, emergency norms push the ruler toward tyranny;
# rights of estates, courts, city immunities pull him toward liberalism)
TYRANNY_KINDS = {"ban", "censorship", "emergency", "corvee", "levy",
                 "confiscation", "proscription"}
LIBERTY_KINDS = {"rights", "court", "immunity", "charter", "assembly",
                 "property", "appeal"}


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def form_factions(civ_id: str, profile: dict[str, float] | None) -> list[EliteFaction]:
    """§22.2.1: build factions from the economy; powers normalized."""
    prof = {**DEFAULT_ECONOMY_PROFILE, **(profile or {})}
    shares: dict[str, float] = {}
    for sector, weight in prof.items():
        order = SECTOR_TO_ORDER.get(sector)
        if order is None or weight <= 0.0:
            continue
        shares[order] = shares.get(order, 0.0) + float(weight)
    total = sum(shares.values()) or 1.0
    factions: list[EliteFaction] = []
    for name in sorted(shares):
        power = shares[name] / total
        factions.append(EliteFaction(
            id=f"{civ_id}:{name}", civilization_id=civ_id, name=name,
            power=power, wealth_stock=power * 10.0,
            loyalty=clamp(0.2 - 0.4 * power, -1.0, 1.0),  # great men fear the throne
            ambition=clamp(power * 0.8, 0.0, 1.0),
            ideology=ORDER_IDEOLOGY.get(name, Ideology.TRADITIONALIST),
        ))
    return factions


def ensure_ruler(civ_id: str, civ_name: str, year: float) -> RulerState:
    """A Rev-3 world gains a neutral incumbent when politics switches on."""
    return RulerState(name=f"Правитель {civ_name or civ_id}",
                      ascended_year=year)


def _suppression_capacity(f: EliteFaction, scale: float) -> float:
    return scale * f.power


def update_factions(factions: list[EliteFaction], ruler: RulerState,
                    years: float, tax_load: float, cfg) -> list[dict]:
    """§22.2.2 per-turn dynamics; returns canonical delta log (admin audit).

    Fear (style > 0) suppresses grievance quickly but only up to the
    faction's fear capacity; beyond it grievance explodes. Liberal rule
    slowly earns loyalty from stability but cannot smother a fresh surge.
    """
    e = cfg.elite
    deltas: list[dict] = []
    tyranny = max(0.0, ruler.style)
    for f in factions:
        g_gain = (e.grievance_tax_rate * tax_load
                  + 0.002 * abs(tyranny) * (f.ideology == Ideology.LIBERAL)) \
            * years
        suppression = tyranny * 0.9 * years
        cap = _suppression_capacity(f, e.fear_capacity_scale) * years
        applied = min(suppression, cap)
        overflow = suppression - applied
        if overflow > 0.0:
            # fear exhausted: the remainder doubles back as fury
            g_gain += overflow * 3.0
        if not f.hostile:
            f.grievance = clamp(f.grievance + g_gain - applied, 0.0, 1.0)
        else:
            f.grievance = clamp(f.grievance + g_gain, 0.0, 1.0)
        l_gain = (e.loyalty_stability_gain * max(0.0, 1.0 - f.grievance)
                  - e.loyalty_stability_gain * 2.0 * f.grievance) * years
        f.loyalty = clamp(f.loyalty + l_gain, -1.0, 1.0)
        f.ambition = clamp(f.ambition
                           + (f.power - f.ambition) * e.ambition_power_gain * years,
                           0.0, 1.0)
        deltas.append({"faction": f.id, "loyalty_delta": round(l_gain, 6),
                       "grievance_delta": round(g_gain - applied, 6)})
    return deltas


def register_cruelty(ruler: RulerState, factions: list[EliteFaction],
                    amount: float, cfg) -> bool:
    """§22.2.2 cruelty_accumulator: repeated repression compounds; past the
    threshold the offended orders become irreversibly hostile. Returns True
    if any faction crossed the point of no return this action."""
    ruler.cruelty_accumulator = clamp(
        ruler.cruelty_accumulator + amount, 0.0, 2.0)
    crossed = False
    if ruler.cruelty_accumulator >= cfg.elite.cruelty_hostile_threshold:
        for f in factions:
            if not f.hostile and f.grievance >= 0.4:
                f.hostile = True
                f.loyalty = min(f.loyalty, -0.6)
                crossed = True
    return crossed


def drift_style(ruler: RulerState, factions: list[EliteFaction], years: float,
                cfg) -> None:
    """§22.2.4: the style axis drifts under elite pressure — the strongest
    order pulls the ruler toward its own temperament."""
    if not factions:
        return
    dominant = max(factions, key=lambda f: f.power)
    target = {"authoritarian": 1.0, "traditionalist": 0.2,
              "liberal": -0.6}[dominant.ideology.value]
    ruler.style = clamp(ruler.style
                        + (target - ruler.style) * cfg.elite.style_drift_per_year
                        * years, -1.0, 1.0)


def conspiracy_pressure(factions: list[EliteFaction], threshold: float) -> float:
    """§22.2.3: Σ power_i · max(0, −loyalty_i − threshold)."""
    return sum(f.power * max(0.0, -f.loyalty - threshold) for f in factions)


def instigator(factions: list[EliteFaction]) -> EliteFaction | None:
    """Plot leader = argmax power·(−loyalty)·ambition among non-loyal orders."""
    candidates = [f for f in factions if f.loyalty < 0.0]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.power * (-f.loyalty) * f.ambition)


def resolve_conspiracy(rng, turn_number: int, civ_id: str, ruler: RulerState,
                       factions: list[EliteFaction], pressure: float,
                       year: float, cfg) -> tuple:
    """Step 17a: ignite-check, success roll, mechanical succession (§22.2.3).

    Both rolls are hidden Fortune draws; raw values go to the returned audit
    dict (server snapshot / admin endpoint ONLY). On RULER_OVERTHROWN the
    winner is picked by weights power × circumstance-roll — the player has
    no lever here by design (decision #2 in todo.md).
    """
    c = cfg.conspiracy
    ignition_p = clamp(c.base_ignition_p * (pressure / c.threshold), 0.0, 0.9)
    ig_value = rng.next_float(turn_number, f"conspiracy:{civ_id}")
    audit = {"ignition_roll": ig_value, "ignition_p": ignition_p}
    new_ruler = None
    if ig_value >= ignition_p:
        return None, audit, None

    leader = instigator(factions)
    if leader is None:
        return None, audit, None

    plotters = [f for f in factions if f.loyalty < 0.0 or f.hostile]
    plot_power = sum(f.power for f in plotters)
    unity = len(plotters) / max(1, len(factions))
    fresh_cruelty = ruler.cruelty_accumulator          # fear still protects…
    p_success = clamp(
        0.5 + 0.4 * plot_power + 0.2 * unity
        - c.success_mod_statecraft * ruler.statecraft
        - c.fresh_cruelty_protection * min(fresh_cruelty, 1.0), 0.05, 0.95)
    su_value = rng.next_float(turn_number, f"conspiracy_success:{civ_id}")
    audit.update({"success_roll": su_value, "success_p": p_success,
                  "leader": leader.id})

    if su_value >= p_success:
        ev = ConspiracyEvent(id=str(uuid.uuid4()), civilization_id=civ_id,
                             turn_number=turn_number,
                             instigator_faction=leader.name,
                             pressure=round(pressure, 4),
                             outcome=ConspiracyOutcome.PLOT_FAILED, year=year)
        # discovery breeds resentment but the throne stands
        leader.grievance = clamp(leader.grievance + 0.2, 0.0, 1.0)
        leader.loyalty = clamp(leader.loyalty - 0.2, -1.0, 1.0)
        return ev, audit, None

    # --- victory of the plot: MECHANICAL succession ------------------------
    weights = []
    for f in plotters or factions:
        circumstance = rng.next_float(turn_number, f"succession:{civ_id}",
                                      index=len(weights))
        weights.append(max(f.power, 1e-9) * (0.5 + circumstance))
    total_w = sum(weights) or 1.0
    probs = [w / total_w for w in weights]
    pick_value = rng.next_float(turn_number, f"succession_pick:{civ_id}")
    acc, winner_idx = 0.0, 0
    for i, p in enumerate(probs):
        acc += p
        if pick_value < acc:
            winner_idx = i
            break
    else:
        winner_idx = len(probs) - 1
    winner = (plotters or factions)[winner_idx]
    audit["succession_pick"] = pick_value
    audit["winner"] = winner.id

    new_style = {"authoritarian": 0.7, "traditionalist": 0.25,
                 "liberal": -0.55}[winner.ideology.value]
    new_ruler = RulerState(
        name=f"Новый правитель от {winner.name}",
        style=new_style, statecraft=0.0,           # reset on accession
        cruelty_accumulator=max(0.0, ruler.cruelty_accumulator - 0.5),
        dao_balance=0.0, ascended_year=year, origin_faction=winner.name)
    # partial treasury confiscation + loyalty reshuffle (§22.2.3)
    for f in factions:
        if f is winner:
            f.loyalty = clamp(max(f.loyalty, 0.3) + 0.3, -1.0, 1.0)
        elif f.ideology == winner.ideology:
            f.loyalty = clamp(f.loyalty + 0.2, -1.0, 1.0)
        else:
            f.loyalty = clamp(f.loyalty - 0.3, -1.0, 1.0)
        f.wealth_stock *= 0.85 if f is not winner else 1.1

    ev = ConspiracyEvent(id=str(uuid.uuid4()), civilization_id=civ_id,
                         turn_number=turn_number,
                         instigator_faction=winner.name,
                         pressure=round(pressure, 4),
                         outcome=ConspiracyOutcome.RULER_OVERTHROWN,
                         new_ruler=new_ruler.name, new_style=new_style,
                         year=year)
    return ev, audit, new_ruler
