"""Rev 4 §22.4 — opponent agency: markets, niches, influence fields.

TASK-074. Rival civilizations stop being static background parameters:
each is steered by an «internal ruler» sharing the §22.2 style model, and
they compete for export markets on the player's own terms — the same
hidden Fortune rolls, the same elite logic, no scripts.

Mechanics implemented here (all resolved INSIDE a turn step — design rule
§22.4-5: «никаких фоновых процессов вне хода»):

 * **Empty niches with a decreasing first-mover bonus (§22.4-2).** A niche
   (sector × region) opens empty; whoever enters first pays only the base
   risk premium; every additional entrant raises saturation, and the entry
   cost grows geometrically — «если игрок медлит — ниша закрывается, и
   войти позже можно только по полной цене риска».
 * **Hidden AI bids for sales markets (§22.4-1).** Exporters of a civ stake
   a bid = capacity × demand × (1 − risk_premium) × style_factor drawn from
   the world RNG. The winner takes the share; losers accumulate
   ``market_grievance``, which feeds back into faction grievance (§22.2).
   The player participates through trade laws but NEVER sees rivals' bids.
 * **influence_field (§22.4-3).** Border regions carry an influence vector
   per civ; long presence, treaties and folklore export shift it. No
   instant conquests — slow drift only, in game time.
 * **Diplomacy with character (§22.4-4).** A tyrannic internal ruler breaks
   treaties more often (fear over reputation); a liberal one values his
   word. Only the statistical tendency is predictable — each specific roll
   stays hidden.

Determinism contract (§22.1): every draw comes from ``WorldRng`` keyed by
(world seed, turn number, civ, purpose) — same seed & history ⇒ same
market shifts (§22.9-8/10). While ``politics.enabled=false`` nothing here
runs at all (Rev-3 identity, §22.9-3).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Sectors that can host an export niche (§22.4). Deterministic order —
#: iteration over this tuple defines slot indices, never reorder lightly.
NICHE_SECTORS: tuple[str, ...] = (
    "grain", "timber", "metals", "textiles", "spices", "horse",
)

#: Regions of the border map (§22.4-3). Also order-significant.
REGIONS: tuple[str, ...] = (
    "north marches", "river delta", "coastal belt", "highland passes",
)

#: How many extra entrants fully close a niche to cheap re-entry.
FULL_SATURATION_ENTRANTS = 4

#: Base price of entering an empty niche (administrative budget units).
BASE_ENTRY_COST = 10.0


@dataclass
class MarketNiche:
    """One sector × region export market (§22.7 ``MarketSlot``).

    ``share_by_civ`` maps civilization id → exported share (canonical,
    server-side). ``bids_last_turn`` are HIDDEN raw stakes — they belong to
    the admin audit only, never to a client payload (§22.8, test §22.9-2).
    """

    sector: str
    region: str
    demand: float = 1.0                 # scale of the whole market
    saturation: float = 0.0             # 0..1, grows with entrants/volume
    entrants: dict[str, float] = field(default_factory=dict)  # civ→capacity
    share_by_civ: dict[str, float] = field(default_factory=dict)
    bids_last_turn: dict[str, float] = field(default_factory=dict)

    @property
    def niche_id(self) -> str:
        return f"{self.sector}@{self.region}"

    def entry_cost(self, risk_premium: float) -> float:
        """§22.4-2: first mover pays base×(1+premium); every occupant makes
        the door dearer — geometric closing of the niche."""
        n = len(self.entrants)
        return BASE_ENTRY_COST * (1.0 + max(0.0, risk_premium)) \
            * (1.0 + self.saturation) ** n

    def is_open(self) -> bool:
        return self.saturation < 0.999

    def to_public_json(self) -> dict:
        """Client-safe view: openness band and who exports — never bids."""
        if self.saturation >= 0.75:
            band = "crowded"
        elif self.saturation >= 0.35:
            band = "contested"
        else:
            band = "open"
        return {"niche": self.niche_id, "sector": self.sector,
                "region": self.region, "saturation_band": band,
                "exporters": sorted(self.share_by_civ), "canonical": True}


def build_default_niche_map() -> dict[str, MarketNiche]:
    """Every sector × region starts as an open niche (§22.3 niche_map)."""
    return {f"{s}@{r}": MarketNiche(sector=s, region=r)
            for s in NICHE_SECTORS for r in REGIONS}


# ---------------------------------------------------------------------------
# Internal AI ruler (§22.4): same style axis as §22.2, simplified model
# ---------------------------------------------------------------------------

@dataclass
class InternalRuler:
    """The hidden helmsman of one AI civilization (§22.4 preamble).

    Shares the liberal↔tyrant axis of ``RulerState.style`` so diplomacy
    «has character»: tyrants weigh fear over reputation, liberals keep
    their word (§22.4-4).
    """

    civ_id: str
    style: float = 0.0            # −1 liberal .. +1 tyrant
    statecraft: float = 0.0       # damps reckless bidding
    export_capacity: float = 1.0  # economic weight behind bids


def make_internal_ruler(civ_id: str, ruler=None, economy_capacity: float = 1.0
                        ) -> InternalRuler:
    """Derive the internal ruler from the civ's public RulerState (§22.2.4).

    AI civs without a seated ruler get a moderate baseline — behaviour is
    then statistically neutral but still deterministic per seed."""
    style = float(ruler.style) if ruler is not None else 0.0
    statecraft = float(ruler.statecraft) if ruler is not None else 0.0
    return InternalRuler(civ_id=civ_id, style=style, statecraft=statecraft,
                         export_capacity=max(0.05, economy_capacity))


def _bid_multiplier(style: float) -> float:
    """Tyrants overreach (prestige-driven stakes); liberals bid sober."""
    return 1.0 + 0.35 * max(0.0, style) - 0.15 * max(0.0, -style)


def place_bids(rng, turn_number: int, niche: MarketNiche,
               rulers: list[InternalRuler]) -> dict[str, float]:
    """§22.4-1: hidden stakes for one occupied-or-open niche.

    bid = capacity · demand · (1 − risk drag) · style factor · noise-roll.
    Pure function of (seed, turn, niche, occupant set) — replay-stable."""
    bids: dict[str, float] = {}
    for r in rulers:
        noise = rng.next_float(turn_number, f"market_bid:{niche.niche_id}",
                               index=_stable_index(r.civ_id))
        # risk drag is a deterministic function of the ruler's own numbers —
        # never of Python's randomized str hash (replay contract §22.1).
        risk_drag = min(0.6, 0.3 + 0.3 * abs(math.sin(
            _stable_index(r.civ_id) / 163.0)))
        bid = (r.export_capacity * niche.demand
               * (1.0 - risk_drag * 0.5)
               * _bid_multiplier(r.style)
               * (0.6 + 0.8 * noise))
        bids[r.civ_id] = round(max(0.0, bid), 6)
    return bids


def resolve_market(rng, turn_number: int, niche: MarketNiche,
                   bidders: list[InternalRuler], risk_premiums: dict[str, float]
                   ) -> dict:
    """Resolve competition inside ONE niche for this turn.

    Winner takes the dominant share proportional to its bid; other
    occupants keep residual shares scaled by their bids. Losers accrue
    ``market_grievance`` (returned in the shift record, §22.4-1). Returns a
    JSON-safe shift record; bids stay in ``niche.bids_last_turn`` (hidden).
    """
    if not bidders or not niche.is_open():
        return {"niche": niche.niche_id, "winner": None, "shares": {},
                "grievances": {}, "skipped": "closed" if not niche.is_open()
                else "no bidders"}
    bids = place_bids(rng, turn_number, niche, bidders)
    niche.bids_last_turn = bids
    total = sum(bids.values()) or 1.0
    winner = max(bids, key=lambda c: bids[c])
    shares = {c: round(b / total, 4) for c, b in bids.items()}
    grievances = {c: round(1.0 - bids[c] / total, 4)
                  for c in bids if c != winner}
    niche.share_by_civ = shares
    # occupancy saturates the niche: each entrant closes the door further
    for c in bids:
        niche.entrants.setdefault(c, next((r.export_capacity for r in bidders
                                           if r.civ_id == c), 1.0))
    load = sum(niche.entrants.values())
    niche.saturation = min(1.0, len(niche.entrants) / FULL_SATURATION_ENTRANTS
                           + 0.05 * load / max(1.0, niche.demand * 10))
    return {"niche": niche.niche_id, "winner": winner, "shares": shares,
            "grievances": grievances,
            "saturation": round(niche.saturation, 4)}


# ---------------------------------------------------------------------------
# influence_field (§22.4-3)
# ---------------------------------------------------------------------------

@dataclass
class InfluenceRegion:
    """Border region with one influence value per civ; slow drift only."""

    region: str
    field: dict[str, float] = field(default_factory=dict)

    def dominant(self) -> str | None:
        if not self.field:
            return None
        return max(self.field, key=lambda c: (self.field[c], c))

    def to_public_json(self) -> dict:
        dom = self.dominant()
        return {"region": self.region, "dominant": dom,
                "presence_band": ("strong" if dom and self.field[dom] >= 0.5
                                  else "weak" if dom else "unsettled"),
                "canonical": True}


def build_influence_map() -> dict[str, InfluenceRegion]:
    return {r: InfluenceRegion(region=r) for r in REGIONS}


def update_influence(regions: dict[str, InfluenceRegion], civ_ids: list[str],
                     years: float, treaty_pairs: list[tuple[str, str]],
                     folklore_export: dict[str, float]) -> None:
    """Presence grows slowly with time; treaties and cultural export shift
    neighbours' fields (§22.4-3). Game-time function only (§20.4)."""
    rate = 0.01 * years                      # «долгое присутствие»
    for reg in regions.values():
        for cid in civ_ids:
            reg.field[cid] = reg.field.get(cid, 0.0) + rate
        for a, b in treaty_pairs:
            if a in reg.field and b in reg.field:
                mutual = min(reg.field[a], reg.field[b]) * 0.02 * years
                reg.field[a] += mutual
                reg.field[b] += mutual
        for cid, export in folklore_export.items():
            if cid in reg.field:
                reg.field[cid] += export * 0.01 * years


# ---------------------------------------------------------------------------
# Diplomacy with character (§22.4-4): hidden break rolls
# ---------------------------------------------------------------------------

def treaty_break_probability(style: float, base: float = 0.05) -> float:
    """Tyrants treat reputation as fear currency — they break words more
    often; liberals barely do. Linear in the −1..+1 style axis."""
    return max(0.0, min(0.9, base * (1.0 + 2.5 * max(0.0, style))
                        + 0.02 * min(0.0, style)))


def maybe_break_treaty(rng, turn_number: int, pair: tuple[str, str],
                       rulers: dict[str, InternalRuler]) -> dict | None:
    """ONE hidden roll per treaty per turn. Returns a chronicle-ready fact
    (without numbers) when the more reckless side breaks the word."""
    a, b = pair
    ra, rb = rulers.get(a), rulers.get(b)
    if ra is None or rb is None:
        return None
    breaker = ra if ra.style >= rb.style else rb
    p = treaty_break_probability(breaker.style)
    value = rng.next_float(turn_number, f"treaty_break:{a}:{b}")
    if value >= p:
        return None
    return {"kind": "TreatyBroken", "breaker": breaker.civ_id,
            "treaty": [a, b],
            "title": "Договор нарушен",
            "detail": "Слово оказалось дешевле выгоды текущего хода."}


# ---------------------------------------------------------------------------
# Step 16a glue: resolve the whole market layer for one turn (§22.4-5)
# ---------------------------------------------------------------------------

def resolve_markets(rng, turn_number: int, niche_map: dict[str, MarketNiche],
                    rulers: dict[str, InternalRuler],
                    trade_interest: dict[str, list[str]],
                    risk_premiums: dict[str, float]) -> dict:
    """Pipeline step 16a: run every niche once; collect public shifts and
    hidden audit. ``trade_interest`` maps civ → sectors it exports into.

    Deterministic: depends only on (seed, turn, current map state)."""
    shifts: list[dict] = []
    hidden_bids: dict[str, dict] = {}
    grievances_by_civ: dict[str, float] = {}
    for niche_id, niche in sorted(niche_map.items()):
        contenders = [rulers[c] for c, sectors in trade_interest.items()
                      if niche.sector in sectors and c in rulers]
        # incumbents always defend their share
        for cid in list(niche.share_by_civ):
            if cid in rulers and rulers[cid] not in contenders:
                contenders.append(rulers[cid])
        contenders.sort(key=lambda r: r.civ_id)
        if not contenders:
            continue
        shift = resolve_market(rng, turn_number, niche, contenders,
                               risk_premiums)
        shifts.append(shift)
        hidden_bids[niche_id] = dict(niche.bids_last_turn)
        for loser, g in shift.get("grievances", {}).items():
            grievances_by_civ[loser] = grievances_by_civ.get(loser, 0.0) + g
    return {"shifts": shifts,
            "public": [n.to_public_json() for n in niche_map.values()],
            "hidden": {"bids": hidden_bids},
            "grievances": grievances_by_civ}


def hash_seedless(s: str) -> int:
    """Stable small integer from a string (never Python's randomized hash)."""
    acc = 5381
    for ch in s:
        acc = ((acc * 33) + ord(ch)) & 0xFFFFFFFF
    return acc


def _stable_index(civ_id: str) -> int:
    return hash_seedless(civ_id) % 1024
