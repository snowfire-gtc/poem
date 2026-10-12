"""Language, koine & dialect dynamics (TASK-068).

One living language per civilization. The capital's speech (the *koine*)
is the root of a dialect tree; isolated communities (mountains, marshes,
deserts, islands) drift away from it and eventually branch off as new
dialects. Transport technology — roads, harbours, later railways, radio
and the net — pulls everything back toward the standard.

Gameplay loop this creates:

* Expansion into hard terrain fragments the language → ``language_unity``
  drops → administrative load rises and law changes get more expensive
  (poem.pricing «языковая раздробленность»).
* Investment in infrastructure and era technologies unifies speech →
  administration gets cheaper, reports read in one common style.
* A unified country that conquers mountain villages inherits a stubborn
  local tongue; the koine may even absorb a neighbouring language after
  a long peace (loanword/merger events in the chronicle).

Design rules honoured here:

* GAME time primary (§20.9): every rate is annual and scaled by the turn
  span, so a 40-year ancient turn moves the language far more than a
  1-year modern turn. Wall-clock time of day never enters (§20.4).
* Determinism (§4.1): all choices are keyed by stable ids and game years;
  no RNG state, no clock reads.
* Pure functions over dataclasses passed in by the engine — this module
  imports nothing from turn/api to avoid cycles.

Tuning constants (single place for balance review):

    ANNUAL_DRIFT_RATE     base yearly divergence gain of an isolated speech
    UNIFY_ANNUAL_RATE     yearly pull of the koine through transport/admin
    SPLIT_THRESHOLD       divergence at which a community becomes its own
                          recorded dialect (branch of the tree)
    MERGE_THRESHOLD       divergence below which two branches re-coalesce
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ``Civilization`` lives in poem.chronology; core.py only forward-declares it
# via the CivilizationId alias.  Importing chronology at module top would be
# fine today, but keeping the import lazy inside a TYPE_CHECKING block plus a
# runtime fallback makes this module import-order-proof (turn.py imports both).
try:  # pragma: no cover - trivial import guard
    from .chronology import Civilization
except Exception:  # circular-import safety net
    from typing import TYPE_CHECKING
    if not TYPE_CHECKING:
        Civilization = object  # type: ignore[assignment,misc]

from .core import TERRAIN_ISOLATION, Dialect, Era, Settlement, SimDuration, Terrain

# --- balance knobs ----------------------------------------------------------

#: Yearly divergence gain of a fully cut-off community (per unit isolation).
ANNUAL_DRIFT_RATE = 0.0016
#: Yearly koine pull per unit of effective connectivity (transport × admin).
UNIFY_ANNUAL_RATE = 0.0055
#: Roads grow with settlement population pressure and civ development.
ROAD_GROWTH_PER_YEAR = 0.0009
#: Divergence beyond which the variety is recorded as a separate dialect.
SPLIT_THRESHOLD = 0.35
#: When unification drags a branch below this, it merges back into the parent.
MERGE_THRESHOLD = 0.06
#: Unity below this level makes low-cohesion areas candidates for a NEW
#: dialect split (used when a plain settlement drifts without a recorded
#: dialect yet).
MIN_UNITY_FOR_STABLE_SPEECH = 0.75
#: Latent isolation above which an unrecorded mountain/island community
#: branches off from the koine in one dramatic turn.
LATENT_SPLIT_LEVEL = 0.45
#: A newly founded seat in hard terrain starts already half-estranged:
#: multiplier on terrain isolation applied as its seed divergence.
FOUNDING_SEED_FACTOR = 0.8
#: How fast the language census reacts to change (fraction per game-year).
UNITY_RESPONSE_PER_YEAR = 0.08
#: Census/unity movement large enough to deserve a chronicle entry.
UNITY_EVENT_DELTA = 0.05

#: Era-driven reach of the standard speech beyond physical roads. Each entry
#: is a multiplicative bonus on connectivity once the civ reached that era
#: (printing presses unify provinces; radio and TV finish the job).
ERA_CONNECTIVITY_BONUS: dict[Era, float] = {
    Era.DAWN: 0.0,
    Era.ANCIENT: 0.02,
    Era.CLASSICAL: 0.05,
    Era.MEDIEVAL: 0.08,
    Era.RENAISSANCE: 0.15,      # print shops
    Era.INDUSTRIAL: 0.30,       # railways, telegraph
    Era.MODERN: 0.50,           # radio, cinema
    Era.ATOMIC: 0.65,           # television
    Era.INFORMATION: 0.85,      # internet
    Era.PLANETARY: 1.00,        # instant everywhere
}

#: How much of the era bonus a community can actually receive depending on
#: terrain — mountains keep their tongue longer even under TV antennas.
ERA_LEAK_BY_TERRAIN: dict[Terrain, float] = {
    Terrain.PLAIN: 1.0,
    Terrain.HILLS: 0.8,
    Terrain.ISLAND: 0.7,
    Terrain.MARSH: 0.55,
    Terrain.DESERT: 0.5,
    Terrain.MOUNTAIN: 0.35,
}


def transport_tech_level(civ: Civilization, era: Era) -> float:
    """Effective reach of the standard speech for one civilization (0..1+).

    Combines the era's communication technology with the civilization's own
    development index: a medieval civ that stagnated administratively spreads
    its koine no further than a strong classical one. Capped at 1.0.
    """
    base = ERA_CONNECTIVITY_BONUS.get(era, 0.0)
    return min(1.0, base * (0.4 + 0.6 * max(0.0, civ.progress)))


def effective_connectivity(settlement: Settlement, civ: Civilization,
                           era: Era) -> float:
    """How strongly the koine reaches one settlement (0..1).

    Physical roads (settlement.infrastructure) times terrain conductance,
    plus the era's mass-media bonus leaking through the same terrain.
    Isolation is the terrain's resistance to common speech.
    """
    iso = TERRAIN_ISOLATION.get(settlement.terrain, 0.3)
    conductance = 1.0 - iso
    roads = min(max(settlement.infrastructure, 0.0), 1.0)
    media = transport_tech_level(civ, era) * ERA_LEAK_BY_TERRAIN.get(
        settlement.terrain, 0.6)
    return min(1.0, roads * conductance + media)


@dataclass
class LanguageState:
    """Per-civilization language bookkeeping owned by the TurnEngine."""

    civilizations: dict[str, Civilization] = field(default_factory=dict)
    settlements: list[Settlement] = field(default_factory=list)
    dialects: dict[str, Dialect] = field(default_factory=dict)
    #: root (koine) dialect id per civilization
    koine_of: dict[str, str] = field(default_factory=dict)
    #: per-turn event log consumed by the chronicle/report layer
    last_events: list[dict] = field(default_factory=list)

    # ---------------------------------------------------------------- setup

    def add_civilization(self, civ: Civilization, era: Era,
                         current_year: float) -> Dialect:
        """Register a civ: create its koine and seed initial settlement speech.

        Existing settlements not yet assigned a variety inherit the koine.
        """
        self.civilizations[civ.id] = civ
        koine = Dialect(id=f"{civ.id}-koine", civilization_id=civ.id,
                        name=f"{civ.name or civ.id}ский койне",
                        parent_id=None, divergence=0.0,
                        created_year=current_year)
        self.dialects[koine.id] = koine
        self.koine_of[civ.id] = koine.id
        for s in self.settlements:
            if s.civilization_id == civ.id and s.dialect_id is None:
                s.dialect_id = koine.id
        return koine

    def add_settlement(self, s: Settlement, era: Era,
                       current_year: float) -> Dialect:
        """Found/annex a settlement; assign its first recorded variety.

        A brand-new isolated foundation immediately speaks the koine but
        drifts fast; annexation of a foreign seat keeps whatever variety it
        already had (its divergence was earned elsewhere).
        """
        self.settlements.append(s)
        civ = self.civilizations.get(s.civilization_id)
        if s.dialect_id is not None and s.dialect_id in self.dialects:
            return self.dialects[s.dialect_id]
        if civ is None:
            # Unknown civ: synthesize a koine so callers always get a dialect.
            civ = Civilization(id=s.civilization_id, name=s.civilization_id)
            self.add_civilization(civ, era, current_year)
        iso = TERRAIN_ISOLATION.get(s.terrain, 0.3)
        seed = min(iso * FOUNDING_SEED_FACTOR, SPLIT_THRESHOLD * 0.9)
        if seed < MERGE_THRESHOLD:
            # Open ground: the new seat simply joins the koine.
            kid = self.koine_of[civ.id]
            s.dialect_id = kid
            return self.dialects[kid]
        return self._split_branch(civ, parent=self.dialects[self.koine_of[civ.id]],
                                  seed=round(seed, 4),
                                  year=current_year, settlement=s)

    def dialect_of(self, settlement: Settlement) -> Dialect | None:
        return self.dialects.get(settlement.dialect_id) if settlement.dialect_id else None

    def koine(self, civ_id: str) -> Dialect | None:
        did = self.koine_of.get(civ_id)
        return self.dialects.get(did) if did else None

    def report_dialect(self, settlement: Settlement) -> str:
        """Human-readable variety label used by report stylisation."""
        d = self.dialect_of(settlement)
        if d is None:
            return "койне"
        if d.parent_id is None:
            return "койне"
        return f"{d.name} ({d.divergence:.2f})"

    # ------------------------------------------------------------- tick core

    def tick(self, span: SimDuration, era: Era, current_year: float) -> list[dict]:
        """Advance the whole language system by one turn interval.

        Returns the list of chronicle-worthy events produced this turn:
        ``DialectSplit``, ``DialectMerge``, ``EraUnification``. Spread/drift
        scale linearly with the GAME-time span (§20.9); short modern turns
        barely move speech, long ancient ones reshape it.
        """
        events: list[dict] = []
        years = max(span.years, 0.0)
        if years <= 0.0:
            self.last_events = events
            return events

        for civ in self.civilizations.values():
            if not getattr(civ, "active", True):
                continue
            seats = [s for s in self.settlements
                     if s.civilization_id == civ.id]
            if not seats:
                continue
            total_pop = sum(s.pop for s in seats) or 1.0

            # 1. Roads slowly grow where there is population and capacity;
            #    they decay slightly in neglected corners (empires shrink too).
            for s in seats:
                growth = ROAD_GROWTH_PER_YEAR * years * (0.5 + civ.progress)
                s.infrastructure = min(1.0, max(0.0, s.infrastructure + growth))

            # 2. Per-settlement drift/unification of its recorded variety.
            #    Koine speakers also erode: a seat deep in hostile terrain,
            #    cut off from roads and media, slowly loses the standard
            #    speech until it crosses the split threshold and is recorded
            #    as a brand-new dialect branching off the koine.
            weighted_unity = 0.0
            for s in seats:
                conn = effective_connectivity(s, civ, era)
                iso = TERRAIN_ISOLATION.get(s.terrain, 0.3)
                d = self.dialect_of(s)
                if d is None:
                    d = self.koine(civ.id)
                    if d is None:
                        continue
                    s.dialect_id = d.id
                if d.parent_id is None:
                    latent = (1.0 - conn) * iso
                    if latent >= LATENT_SPLIT_LEVEL:
                        child = self._split_branch(
                            civ, parent=d, seed=SPLIT_THRESHOLD * 0.6,
                            year=current_year, settlement=s)
                        events.append({
                            "kind": "DialectSplit", "year": current_year,
                            "civilization_id": civ.id,
                            "dialect_id": child.id, "from_id": d.id,
                            "title": f"Рождение говора «{child.name}»",
                            "detail": f"{s.name}: речь ({s.terrain.value}) "
                                      "всё меньше походит на городскую",
                        })
                        d = child
                    else:
                        # Koine speakers simply count toward unity.
                        weighted_unity += s.pop
                        continue
                drift = ANNUAL_DRIFT_RATE * years * (iso + 0.15) \
                    * (1.0 - conn)
                unify = UNIFY_ANNUAL_RATE * years * conn \
                    * (0.5 + 0.5 * civ.language_unity)
                d.divergence = max(0.0, min(1.0, d.divergence + drift - unify))

                if d.divergence <= MERGE_THRESHOLD:
                    parent = self.dialects.get(d.parent_id or "")
                    if parent is not None:
                        events.append({
                            "kind": "DialectMerge", "year": current_year,
                            "civilization_id": civ.id,
                            "dialect_id": d.id, "into_id": parent.id,
                            "title": f"«{d.name}» сливается с "
                                     f"{'койне' if parent.parent_id is None else parent.name}",
                            "detail": (f"{s.name}: речь гор/болот снова звучит "
                                       "как городская"),
                        })
                        for other in seats:
                            if other.dialect_id == d.id:
                                other.dialect_id = parent.id
                        self.dialects.pop(d.id, None)
                        d = parent
                weighted_unity += s.pop * (1.0 - d.divergence)

            # 3. Civilization-level unity index (population-weighted).
            unity = max(0.0, min(1.0, weighted_unity / total_pop))
            prev = civ.language_unity
            # smooth: unity eases toward the census value over game years
            civ.language_unity = prev + (unity - prev) * min(
                1.0, years * UNITY_RESPONSE_PER_YEAR)
            if prev - civ.language_unity > UNITY_EVENT_DELTA:
                events.append({
                    "kind": "EraUnification", "year": current_year,
                    "civilization_id": civ.id, "direction": "fragmentation",
                    "title": "Речь державы дробится",
                    "detail": f"единство языка упало до "
                              f"{civ.language_unity:.2f}",
                })
            elif civ.language_unity - prev > UNITY_EVENT_DELTA:
                events.append({
                    "kind": "EraUnification", "year": current_year,
                    "civilization_id": civ.id, "direction": "unification",
                    "title": "Общее слово расходится по дорогам",
                    "detail": f"единство языка выросло до "
                              f"{civ.language_unity:.2f}",
                })

        self.last_events = events
        return events

    # -------------------------------------------------------------- helpers

    def _split_branch(self, civ: Civilization, parent: Dialect, seed: float,
                      year: float, settlement: Settlement | None = None) -> Dialect:
        n = sum(1 for d in self.dialects.values()
                if d.civilization_id == civ.id) 
        suffix = ""
        if settlement is not None:
            suffix = settlement.name or settlement.terrain.value
        else:
            suffix = civ.name or civ.id
        name = f"{suffix[:24]} говор №{n}"
        d = Dialect(id=f"{civ.id}-dialect-{n}", civilization_id=civ.id,
                    name=name, parent_id=parent.id,
                    divergence=min(seed, SPLIT_THRESHOLD * 0.9),
                    created_year=year)
        self.dialects[d.id] = d
        if settlement is not None:
            settlement.dialect_id = d.id
        return d

    # ----------------------------------------------------------- queries API

    def census(self, civ_id: str) -> dict:
        """Client-facing projection of one civilization's language state."""
        civ = self.civilizations.get(civ_id)
        if civ is None:
            return {}
        seats = [s for s in self.settlements if s.civilization_id == civ_id]
        by_dialect: dict[str, float] = {}
        for s in seats:
            did = s.dialect_id or self.koine_of.get(civ_id, "?")
            by_dialect[did] = by_dialect.get(did, 0.0) + s.pop
        return {
            "civilization_id": civ_id,
            "unity": round(civ.language_unity, 4),
            "koine_id": self.koine_of.get(civ_id),
            "dialects": [
                {"dialect_id": d.id, "name": d.name,
                 "parent_id": d.parent_id,
                 "divergence": round(d.divergence, 4),
                 "speakers": round(by_dialect.get(d.id, 0.0), 1)}
                for d in sorted(self.dialects.values(),
                                key=lambda x: x.id)
                if d.civilization_id == civ_id
            ],
            "settlements": [
                {"settlement_id": s.id, "name": s.name,
                 "terrain": s.terrain.value,
                 "infrastructure": round(s.infrastructure, 4),
                 "dialect_id": s.dialect_id}
                for s in sorted(seats, key=lambda x: x.id)
            ],
        }

    def average_intelligibility_to_koine(self, civ_id: str) -> float:
        """Population-weighted intelligibility of local speech vs the koine.

        Used by pricing/validation: decrees travel poorly where this is low.
        """
        koine = self.koine(civ_id)
        if koine is None:
            return 1.0
        seats = [s for s in self.settlements if s.civilization_id == civ_id]
        total = sum(s.pop for s in seats) or 1.0
        acc = 0.0
        from .core import mutual_intelligibility
        for s in seats:
            acc += s.pop * mutual_intelligibility(koine, self.dialect_of(s))
        return acc / total
