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
    text_summary: str = ""


@dataclass
class CanonicalLawChange:
    """Machine-readable change carried by a revision."""

    action: "LawUserAction"
    new_canonical: CanonicalLaw | None = None   # for AmendLaw / SubmitDraft path
    suspend_years: float | None = None          # for SuspendLaw
    transition: SimDuration | None = None       # optional transition period
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
