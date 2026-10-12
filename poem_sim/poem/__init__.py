"""Poem simulation core — Design Doc Rev 3 implementation.

Modules:
 * poem.core        — domain types (Law, LawDraft, LawRevision, SimDuration, Turn…)
 * poem.laws        — server-side draft/revision store (TASK-061)
 * poem.tokens      — visual identity token registry in server RAM (TASK-067)
 * poem.pricing     — law change cost without time-of-day terms (TASK-060)
 * poem.chronology  — WorldChronology, eras, dynamic turn intervals (TASK-063/064/065)
 * poem.turn        — simulation cutoff, snapshots, turn pipeline (TASK-062)
 * poem.api         — REST handlers per §15
 * poem.config      — TOML configuration loading & validation (§8, §14)
"""

from .core import (  # noqa: F401
    CanonicalLaw,
    CanonicalLawChange,
    CustomaryLaw,
    DraftStatus,
    Era,
    EraSource,
    FolkloreItem,
    GameSpeed,
    Law,
    LawConflict,
    LawDraft,
    LawRevision,
    LawRevisionStatus,
    LawStatus,
    LawUserAction,
    SeasonId,
    SimDuration,
    TimedEffect,
    TimedEffectKind,
    Treaty,
    Turn,
    TurnLawSnapshot,
    TurnStatus,
)
from .chronology import (  # noqa: F401
    Civilization,
    TurnIntervalRule,
    WorldChronology,
    advance_chronology,
    calculate_turn_span,
    determine_world_era,
)
from .laws import LawStore  # noqa: F401
from .validation import (  # noqa: F401
    DEFAULT_VALIDATOR,
    LawValidator,
    ValidationResult,
    detect_kind,
)
from .pricing import PricingConfig, cost_of_law_change  # noqa: F401
from .tokens import TOKEN_KINDS, TokenRegistry, VisualToken  # noqa: F401
from .turn import DEFERRED_NOTICE, TurnEngine, TurnReport  # noqa: F401
from .api import WorldServer  # noqa: F401
from .client import ApiError, HttpClient, LibraryClient  # noqa: F401

__all__ = [
    "CanonicalLaw", "CanonicalLawChange", "CustomaryLaw", "DraftStatus", "Era",
    "EraSource", "FolkloreItem", "GameSpeed", "Law", "LawConflict", "LawDraft", "LawRevision",
    "LawRevisionStatus", "LawStatus", "LawUserAction", "SeasonId", "SimDuration",
    "TimedEffect", "TimedEffectKind", "Treaty", "Turn", "TurnLawSnapshot",
    "TurnStatus", "Civilization", "TurnIntervalRule", "WorldChronology",
    "advance_chronology", "calculate_turn_span", "determine_world_era",
    "LawStore", "DEFAULT_VALIDATOR", "LawValidator", "ValidationResult",
    "detect_kind",
    "TOKEN_KINDS", "TokenRegistry", "VisualToken",
    "PricingConfig", "cost_of_law_change", "DEFERRED_NOTICE",
    "TurnEngine", "TurnReport", "WorldServer",
    "ApiError", "HttpClient", "LibraryClient",
]
