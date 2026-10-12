"""Core domain types for the Poem simulation (Design Doc Rev 3).

Key Rev-3 rules implemented here:
 1. All law edits are stored server-side (drafts + revisions).
 2. The turn simulation uses a deterministic server snapshot of laws.
 3. Real time-of-day NEVER affects gameplay cost/effectiveness — only the
    logical ``simulation_cutoff`` decides which revision joins which turn.
 4. Turns advance world time by a dynamic, Civilization-like interval that
    depends on era and game speed.
 5. All long-duration effects (treaties, transitions, travel) are stored in
    *game time* (SimDuration), not in turn counts.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Identifiers (plain strings; server generates them)
# ---------------------------------------------------------------------------

WorldId = str
CivilizationId = str
LawId = str
LawDraftId = str
LawRevisionId = str
TurnId = str
TreatyId = str


# ---------------------------------------------------------------------------
# Eras / seasons / speeds
# ---------------------------------------------------------------------------

class Era(enum.Enum):
    """Structural era ladder (section 7.3). Names can be reskinned later."""

    DAWN = "dawn"
    ANCIENT = "ancient"
    CLASSICAL = "classical"
    MEDIEVAL = "medieval"
    RENAISSANCE = "renaissance"
    INDUSTRIAL = "industrial"
    MODERN = "modern"
    ATOMIC = "atomic"
    INFORMATION = "information"
    PLANETARY = "planetary"

    @property
    def rank(self) -> int:
        return list(Era).index(self)


ERA_ORDER: tuple[Era, ...] = tuple(Era)


class SeasonId(enum.Enum):
    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"
    # Example of a setting-specific season name used in API samples:
    LONG_NIGHT = "long_night"


class GameSpeed(enum.Enum):
    QUICK = "quick"
    STANDARD = "standard"
    EPIC = "epic"
    MARATHON = "marathon"


class EraSource(str, enum.Enum):
    LEADING_CIVILIZATION = "leading_civilization"
    MEDIAN_ACTIVE_CIVILIZATIONS = "median_active_civilizations"
    WEIGHTED_AVERAGE = "weighted_average"


# ---------------------------------------------------------------------------
# SimDuration — game-time quantity (sections 7.2, 11.x)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SimDuration:
    """A span of in-game time.

    For an alien planet these are *planetary* years/seasons/days; the
    configuration expresses them in conventional units for convenience.
    """

    years: float = 0.0
    seasons: float = 0.0
    days: float = 0.0

    def __add__(self, other: "SimDuration") -> "SimDuration":
        return SimDuration(
            self.years + other.years,
            self.seasons + other.seasons,
            self.days + other.days,
        )

    def scale(self, factor: float) -> "SimDuration":
        return SimDuration(self.years * factor, self.seasons * factor, self.days * factor)

    @property
    def is_expired(self) -> bool:
        return self.years <= 0.0


def apply_speed_multiplier(span: SimDuration, multiplier: float) -> SimDuration:
    """effective_years_per_turn = base_years_per_turn * speed_multiplier (§8.2)."""
    return span.scale(multiplier)


# ---------------------------------------------------------------------------
# Laws
# ---------------------------------------------------------------------------

class LawStatus(enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REPEALED = "repealed"
    IN_TRANSITION = "in_transition"


@dataclass
class CanonicalLaw:
    """Structured, machine-readable form of a law produced by the NLP layer.

    Drafts may lack it until validated; revisions must have it before they
    can be included in a simulation snapshot.
    """

    kind: str                      # e.g. "tax", "corvee", "ban"
    scope: dict = field(default_factory=dict)   # {"population_below": 200} etc.
    effect: dict = field(default_factory=dict)  # parameterised effects
    refs: tuple = ()               # ids of other laws this norm depends on
    text_summary: str = ""


# Pairs of law kinds that contradict each other when both are active for the
# same civilization (§13 step 13 — norm-conflict detection). Symmetric.
CONFLICTING_KINDS: dict[str, set[str]] = {
    "tax": {"ban"},        # e.g. hearth tax vs. ban on resource extraction
    "corvee": {"ban"},     # forced labour vs. prohibition of it
    "levy": {"exemption"}, # conscription vs. blanket exemption
}


def kinds_conflict(kind_a: str, kind_b: str) -> bool:
    """True if two canonical law kinds contradict each other (§5.1 term)."""
    return kind_b in CONFLICTING_KINDS.get(kind_a, set()) or \
        kind_a in CONFLICTING_KINDS.get(kind_b, set())


@dataclass
class CanonicalLawChange:
    """Machine-readable change carried by a revision."""

    action: "LawUserAction"
    new_canonical: CanonicalLaw | None = None   # for AmendLaw / SubmitDraft path
    suspend_years: float | None = None          # for SuspendLaw
    transition: SimDuration | None = None       # optional transition period
    urgent: bool = False                        # §5.2 special-resource urgency,
                                                # NEVER wall-clock time of day
    note: str = ""


class LawUserAction(enum.Enum):
    """§3.2 — types of user law changes."""

    CREATE_DRAFT = "CreateDraft"
    UPDATE_DRAFT = "UpdateDraft"
    SUBMIT_DRAFT = "SubmitDraft"
    AMEND_LAW = "AmendLaw"
    REPEAL_LAW = "RepealLaw"
    SUSPEND_LAW = "SuspendLaw"
    REINSTATE_LAW = "ReinstateLaw"
    CLARIFY_LAW = "ClarifyLaw"
    CANCEL_PENDING_CHANGE = "CancelPendingChange"


@dataclass
class Law:
    id: LawId
    civilization_id: CivilizationId
    title: str
    raw_text: str
    canonical: CanonicalLaw | None
    status: LawStatus = LawStatus.ACTIVE
    tradition: float = 0.0          # entrenchedness in culture (0..1+)
    age_years: float = 0.0          # how long the law has existed (game time)
    enacted_turn: int = 0
    last_revision_seq: int = 0
    complexity: float = 1.0         # affects change cost
    administrative_load: float = 1.0


@dataclass
class CustomaryLaw:
    """Laws that persist as custom even after formal repeal (§4.3 remnants)."""

    id: LawId
    civilization_id: CivilizationId
    title: str
    strength: float = 0.5
    residual_effect: dict = field(default_factory=dict)


@dataclass
class FolkloreItem:
    """A folk narrative/belief tied to a civilization (§11.5).

    Spread and mutation are driven by ANNUAL rates scaled by the turn's
    game-time span — never by real-world time of day (§20.4).
    """

    id: str
    civilization_id: CivilizationId
    title: str
    topic: str = ""                 # e.g. law id or theme it references
    spread: float = 0.1             # how widely known (0..1+)
    mutation: float = 0.0           # accumulated drift from the origin (0..1+)
    oral_strength: float = 1.0      # carrier strength (bards, markets, roads)
    distance_factor: float = 1.0    # isolation slows convergence, speeds drift
    conflict_factor: float = 0.0    # tension with active laws boosts mutation


@dataclass
class LawConflict:
    law_a: LawId
    law_b: LawId | None = None      # None => conflict with folklore/custom
    folklore_ref: str | None = None
    revision_id: str | None = None  # the pending edit that creates this clash
    severity: float = 0.5
    description: str = ""


# ---------------------------------------------------------------------------
# Server-side drafts & revisions (§3.3, §3.4)
# ---------------------------------------------------------------------------

class DraftStatus(enum.Enum):
    EDITING = "Editing"
    READY_FOR_PREVIEW = "ReadyForPreview"
    VALIDATED = "Validated"
    INVALID = "Invalid"
    SUBMITTED = "Submitted"
    DISCARDED = "Discarded"


@dataclass
class LawDraft:
    """§3.3 — drafts live on the server, never affect simulation."""

    id: LawDraftId
    civilization_id: CivilizationId
    target_law_id: LawId | None
    raw_text: str
    canonical_draft: CanonicalLaw | None = None
    status: DraftStatus = DraftStatus.EDITING
    updated_at: datetime = field(default_factory=utcnow)


class LawRevisionStatus(enum.Enum):
    PENDING = "Pending"
    INCLUDED_IN_SIMULATION = "IncludedInSimulation"
    SUPERSEDED = "Superseded"
    REJECTED = "Rejected"
    APPLIED = "Applied"
    FAILED = "Failed"


@dataclass
class LawRevision:
    """§3.4 — confirmed user edit, versioned on the server.

    ``received_at`` is kept purely for auditing. Per TASK-060 it MUST NOT be
    use gameplay modifier (no time-of-day penalties/bonuses).
    """

    id: LawRevisionId
    law_id: LawId
    civilization_id: CivilizationId
    action: LawUserAction
    raw_text: str
    canonical_change: CanonicalLawChange
    sequence_number: int
    received_at: datetime = field(default_factory=utcnow)
    simulation_turn_id: TurnId | None = None
    status: LawRevisionStatus = LawRevisionStatus.PENDING
    cost: float = 0.0
    deferred_to_next_turn: bool = False   # set when made after cutoff (§4.4)


# ---------------------------------------------------------------------------
# Timed effects — everything duration-based lives in GAME TIME (§11.6–11.8)
# ---------------------------------------------------------------------------

class TimedEffectKind(enum.Enum):
    TREATY = "treaty"
    LAW_TRANSITION = "law_transition"
    TRAVEL = "travel"
    SUSPENSION = "suspension"
    WAR = "war"


@dataclass
class War:
    """An ongoing conflict between civilizations (§13 step 17).

    Wars live in GAME time like every other long effect (§20.9): fatigue
    accumulates proportionally to the turn span, so a 40-year ancient turn
    exhausts a society far more than a 1-year modern turn. Nothing here
    depends on real-world time of day (§20.4).
    """

    id: str
    world_id: WorldId
    attacker_id: CivilizationId
    defender_id: CivilizationId
    started_year: float = 0.0
    active: bool = True
    fatigue_attacker: float = 0.0     # accumulated social exhaustion 0..1+
    fatigue_defender: float = 0.0
    annual_fatigue_rate: float = 0.01  # per game-year while active

    def tick(self, span_years: float) -> None:
        """Advance one turn interval; heavy processes are NOT linear (§19.1):
        fatigue gains diminish as exhaustion grows."""
        if not self.active:
            return
        self.fatigue_attacker += self.annual_fatigue_rate * span_years / (
            1.0 + self.fatigue_attacker)
        self.fatigue_defender += self.annual_fatigue_rate * span_years / (
            1.0 + self.fatigue_defender)

    @property
    def exhausted(self) -> bool:
        return self.fatigue_attacker >= 1.0 and self.fatigue_defender >= 1.0


@dataclass
class Prejudice:
    """A collective bias of a civilization (§13 step 15).

    Prejudices live in GAME time like every other long effect (§20.9): they
    intensify while the underlying conflict is alive and fade once it is
    resolved (repealed law, expired treaty, ended war). Nothing here depends
    on real-world time of day (§20.4).
    """

    id: str
    civilization_id: CivilizationId
    title: str
    target: str = ""                 # e.g. folk group, foreign civ, custom
    intensity: float = 0.1           # 0..1+
    annual_growth: float = 0.004     # per game-year while unresolved
    annual_decay: float = 0.002      # per game-year once resolved
    resolved: bool = False


@dataclass
class TimedEffect:
    id: str
    world_id: WorldId
    civilization_id: CivilizationId | None
    kind: TimedEffectKind
    remaining: SimDuration
    payload: dict = field(default_factory=dict)

    def tick(self, span: SimDuration) -> bool:
        """Advance by one turn interval. Returns True if expired."""
        self.remaining = SimDuration(
            years=self.remaining.years - span.years,
            seasons=self.remaining.seasons - span.seasons,
            days=self.remaining.days - span.days,
        )
        return self.remaining.is_expired


@dataclass
class Treaty(TimedEffect):
    treaty_kind: str = "peace"


# ---------------------------------------------------------------------------
# Interval events — chronicle of what happened during one turn span (§19.1)
# ---------------------------------------------------------------------------

# A turn shorter than this produces no intermediate cultural milestones:
# small yearly processes are not faked frame-by-frame (§19.1).
MIN_EVENT_SPAN_YEARS = 5.0
# Approximate spacing of generated culture/folklore milestones inside a long
# interval, so 40-year ancient turns read as a chronicle, not a time jump.
EVENT_INTERVAL_YEARS = 25.0


@dataclass
class IntervalEvent:
    """A chronicle entry covering the whole turn interval (§19.1).

    Key events (era change, treaty expiry, law repeal/suspension, envoy
    arrival) always appear; additionally, evenly spaced cultural-folklore
    milestones are generated inside long intervals so that players do not
    feel entire decades were skipped. Generation is deterministic in game
    time — wall-clock time of day never influences it (§20.4).
    """

    kind: str            # EraChange | Suspension | Repeal | TreatyExpired |
                         # TravelArrival | TransitionEnd | CulturalMilestone
    year: float          # world year at which the event is placed
    title: str
    detail: str = ""

    def to_json(self) -> dict:
        return {"kind": self.kind, "year": round(self.year, 2),
                "title": self.title, "detail": self.detail}


# ---------------------------------------------------------------------------
# Language & dialects (TASK-068) — one living language per civilization
# ---------------------------------------------------------------------------


class Terrain(enum.Enum):
    """Settlement terrain. Connectivity of the land decides how far a
    standard speech can travel: open ground carries it, mountains and marshes
    swallow it, islands carry it by sea lanes (§ language design)."""

    PLAIN = "plain"
    HILLS = "hills"
    MOUNTAIN = "mountain"
    DESERT = "desert"
    MARSH = "marsh"
    ISLAND = "island"


#: Base resistance of the land to the spread of common speech (0..1).
TERRAIN_ISOLATION: dict[Terrain, float] = {
    Terrain.PLAIN: 0.05,
    Terrain.HILLS: 0.25,
    Terrain.MOUNTAIN: 0.70,
    Terrain.DESERT: 0.55,
    Terrain.MARSH: 0.45,
    Terrain.ISLAND: 0.35,
}


@dataclass
class Settlement:
    """A settlement of a civilization — carrier of its local speech.

    ``infrastructure`` is the road/harbour level feeding the transport tech
    (see poem.language.transport_tech_level); ``pop`` weights how much a
    community counts in the language census.
    """

    id: str
    civilization_id: CivilizationId
    name: str = ""
    terrain: Terrain = Terrain.PLAIN
    pop: float = 1000.0
    infrastructure: float = 0.0      # road/harbour level, grows over game time
    dialect_id: str | None = None    # current local speech variety


@dataclass
class Dialect:
    """One speech variety inside a civilization's language tree.

    The capital's koine is the root; mountain/marsh/island communities split
    off as children when isolation outruns unification for long enough.
    All dynamics run in GAME time (§20.9); nothing here consults the
    wall clock (§20.4).
    """

    id: str
    civilization_id: CivilizationId
    name: str
    parent_id: str | None = None     # None => the koine (root of the tree)
    divergence: float = 0.0          # distance from the koine, 0..1
    created_year: float = 0.0        # world year of the split


def mutual_intelligibility(a: Dialect | None, b: Dialect | None) -> float:
    """Intelligibility between two varieties of ONE language, 0..1.

    Same variety: fully understood. Two branches that forked at a common
    ancestor are penalised by the sum of their post-fork drifts — the deeper
    each side wandered since splitting, the harder the conversation. A child
    is always better understood by its parent than two cousins by each other.
    """
    if a is None or b is None:
        return 1.0
    if a.id == b.id:
        return 1.0
    # Divergence is measured from the koine, so two varieties share speech up
    # to their lesser drift; everything above it diverged after their fork.
    # Cousins that wandered far apart are penalised by their difference too.
    drift = abs(a.divergence - b.divergence) + min(a.divergence, b.divergence) * 0.5
    return max(0.05, 1.0 - 0.9 * drift)


# ---------------------------------------------------------------------------
# Turn (§12)
# ---------------------------------------------------------------------------

class TurnStatus(enum.Enum):
    SCHEDULED = "Scheduled"
    COLLECTING_DIRECTIVES = "CollectingDirectives"
    SNAPSHOT_PREPARED = "SnapshotPrepared"
    SIMULATING = "Simulating"
    FINALIZING = "Finalizing"
    FINALIZED = "Finalized"
    FAILED = "Failed"


@dataclass
class Turn:
    id: TurnId
    world_id: WorldId
    number: int
    started_at: datetime
    simulation_cutoff_at: datetime
    finalized_at: datetime | None = None
    status: TurnStatus = TurnStatus.SCHEDULED
    seed: int = 0
    turn_span: SimDuration = field(default_factory=SimDuration)
    world_year_before: float = 0.0
    world_year_after: float = 0.0
    era_before: Era = Era.DAWN
    era_after: Era = Era.DAWN


# ---------------------------------------------------------------------------
# Turn law snapshot (§4.3)
# ---------------------------------------------------------------------------

@dataclass
class TurnLawSnapshot:
    turn_id: TurnId
    civilization_id: CivilizationId
    active_laws: list[Law] = field(default_factory=list)
    pending_amendments: list[LawRevision] = field(default_factory=list)
    pending_repeals: list[LawRevision] = field(default_factory=list)
    suspended_laws: list[LawId] = field(default_factory=list)
    customary_remnants: list[CustomaryLaw] = field(default_factory=list)
    conflicts: list[LawConflict] = field(default_factory=list)
    # Rev 4 (§22.7): hidden Fortune rolls of this turn for this civ. They are
    # server-side audit data ONLY — never serialized into a client payload
    # (§22.1 rule 2; test §22.9-2). Admin raw endpoint may expose them.
    fortune_rolls: list["FortuneRoll"] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Rev 4 (§22): political philosophy layer — «Государь» and «Дао Дэ Цзин».
# All of it is inert while politics.enabled=false (behaviour identical to
# Rev 3 — the hard backward-compatibility constraint, todo.md §Этап 1).
# ---------------------------------------------------------------------------

class Ideology(str, enum.Enum):
    LIBERAL = "liberal"
    AUTHORITARIAN = "authoritarian"
    TRADITIONALIST = "traditionalist"


#: Canonical elite orders from which factions are formed out of the economy
#: (§22.2.1). Order matters only for deterministic iteration.
ELITE_ORDERS: tuple[str, ...] = (
    "clergy", "nobility", "merchants", "military", "scribes", "artisans",
)


@dataclass
class RulerState:
    """The ruling person of one civilization (§22.2.4, §22.7).

    ``style`` is the hidden liberal↔tyrant axis (−1..+1); ``statecraft`` is
    accumulated virtue that tempers Fortune (§22.1 p_eff formula);
    ``cruelty_accumulator`` tracks repeated repression inside a window and
    flips factions irreversibly hostile past its threshold (§22.2.2);
    ``dao_balance`` grows through non-action (ву-вэй, §22.3).
    """

    name: str = ""
    style: float = 0.0                 # −1 liberal .. +1 tyrant
    statecraft: float = 0.0            # 0..1 accumulated virtue/prudence
    cruelty_accumulator: float = 0.0   # fresh repressions, decays with time
    dao_balance: float = 0.0           # accumulated «force of the Way»
    self_deception: float = 0.0        # belief in one's own flattery (§22.5)
    ascended_year: float = 0.0         # world year of coming to power
    origin_faction: str | None = None  # faction that seated this ruler


@dataclass
class EliteFaction:
    """One elite order of a civilization (§22.2.1).

    power/wealth_stock/grievance are canonical server-side numbers; loyalty
    and ambition are HIDDEN from ordinary clients (§22.8, test §22.9-2).
    ``hostile`` is the irreversible state after the cruelty threshold.
    """

    id: str
    civilization_id: CivilizationId
    name: str                          # clergy/nobility/merchants/...
    power: float = 0.0                 # normalized share among the factions
    loyalty: float = 0.0               # −1..1 toward the current ruler
    wealth_stock: float = 0.0          # accumulated economic weight
    grievance: float = 0.0             # 0..1 accumulated resentment
    ambition: float = 0.0              # hidden; grows with power
    ideology: Ideology = Ideology.TRADITIONALIST
    hostile: bool = False              # past the cruelty point of no return

    def to_public_json(self) -> dict:
        """Client-safe view: rough power band and position only — never the
        exact loyalty/grievance/ambition numbers (§22.8)."""
        if self.power >= 0.30:
            band = "high"
        elif self.power >= 0.15:
            band = "medium"
        else:
            band = "low"
        if self.hostile or self.loyalty <= -0.5:
            position = "disaffected"
        elif self.loyalty >= 0.4:
            position = "loyal"
        else:
            position = "watchful"
        return {"faction_id": self.id, "name": self.name,
                "power_band": band, "position": position,
                "ideology": self.ideology.value, "canonical": True}


@dataclass
class FortuneRoll:
    """A single hidden Fortune roll of one turn (§22.1).

    Raw values live in the server snapshot only; the client sees events,
    never numbers (test §22.9-2). Deterministic: same seed ⇒ same rolls
    (test §22.9-1).
    """

    event_id: str                      # e.g. "poor_harvest"
    title: str                         # chronicle-ready subject
    severity: float                    # 0..1 amplitude before dampers
    roll_value: float                  # raw PRNG draw — hidden
    threshold: float                   # effective probability — hidden
    success: bool                      # did the event strike?
    civ_id: str | None = None
    detail: str = ""

    def to_event_json(self) -> dict:
        """Public chronicle projection: consequences only, no numbers."""
        return {"kind": "FortuneEvent", "event_id": self.event_id,
                "title": self.title, "detail": self.detail}


@dataclass
class ConspiracyOutcome(enum.Enum):
    PLOT_DISCOVERED = "plot_discovered"    # suppressed before it ripened
    RULER_OVERTHROWN = "ruler_overthrown"  # mechanical succession (§22.2.3)
    PLOT_FAILED = "plot_failed"            # rose but was defeated


@dataclass
class ConspiracyEvent:
    """Result of step 17a when conspiracy pressure plus a hidden roll ignite
    a plot (§22.2.3). Details of the rolls stay server-side."""

    id: str
    civilization_id: CivilizationId
    turn_number: int
    instigator_faction: str
    pressure: float                    # Σ power·max(0,−loyalty−threshold)
    outcome: ConspiracyOutcome
    new_ruler: str | None = None       # name, when succession happened
    new_style: float | None = None
    year: float = 0.0

    def to_chronicle_json(self) -> dict:
        """Public view: the fact of the plot/succession without the hidden
        mechanics (§22.1 rule 2, decision: player faces fait accompli)."""
        return {
            "kind": "ConspiracyEvent",
            "civilization_id": self.civilization_id,
            "instigator": self.instigator_faction,
            "outcome": self.outcome.value,
            "new_ruler": self.new_ruler,
            "year": round(self.year, 2),
        }
