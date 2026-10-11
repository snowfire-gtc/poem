"""Neural-layer text validation of law drafts (TASK-066, §3.3).

The game's premise (rules.md §1): the player writes laws in natural language
and a server-side neural layer turns them into formal canonical norms and
checks their logical consistency. That layer runs exclusively on the
server's own hardware — no external API is ever called (§20.7); after the
initial weight download the whole pipeline works air-gapped.

This module ships the *deterministic heuristic* implementation of that
layer: a dependency-free lexical analyser that stands in for the local
inference model behind the same ``LawValidator`` protocol, so production
can swap in a GPU-backed encoder without touching call sites. The heuristics
are deliberately stable: identical text must always produce an identical
verdict (the simulation is deterministic, §4.1).

What "validated" means here (§3.3):
 * the draft has enough substance to be interpretable at all;
 * its canonical form was produced (kind + effect parameters);
 * it declares no self-contradiction ("collect X and do not collect X");
 * it is concrete enough to actually generate an effect (§1 «достаточно ли
   он конкретен, чтобы порождать эффект»).

A failing verdict sets ``DraftStatus.INVALID`` and the draft STAYS on the
server, editable from any device (guide_client.md: «черновик остаётся на
сервере в статусе Invalid, его можно править дальше»). Validation never
consults wall-clock time (§2): the same text validated at 03:00 receives
exactly the same verdict as at 18:00.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .core import CanonicalLaw, DraftStatus

# ---------------------------------------------------------------------------
# Lexical resources (Russian + English cue words for the supported kinds)
# ---------------------------------------------------------------------------

_KIND_CUES: dict[str, tuple[str, ...]] = {
    "tax":       ("налог", "налога", "налогов", "облагается", "облагают", "-tax", "tax "),
    "corvee":    ("барщин", "принудительн", "отработк", "труд должен"),
    "levy":      ("призыв", "мобилизац", "рекрут", "воинскую повинность"),
    "ban":       ("запрет", "запрещ", "не допускается", "нельзя", "под страхом"),
    "exemption": ("льгот", "освобожда", "изъят", "exempt"),
    "spending":  ("расход", "финансиров", "субсиди", "выплаты", "стипенди"),
    "right":     ("право", "вправе", "гарантиру", "допускается", "разреш"),
}

# Words signalling the norm is about prohibition even when phrased softly.
_BAN_HINTS = ("запрет", "запрещ", "нельзя", "не допускается")

# Negation cues used for the direct self-contradiction check.
_NEGATIONS = ("не ", "ни ", "без ", "нет ", "nor ", "not ")

# Vague quantifiers: a law built only around them generates no effect (§1).
_VAGUE_WORDS = frozenset(
    word.lower() for word in (
        "некоторые", "немного", "немногие", "слегка", "частично", "примерно",
        "примерное", "кое-какие", "несколько", "по-возможности", "максимально",
        "оптимально", "справедливо", "хорошо", "лучше", "иногда", "порой",
        "немножко", "несколько", "somehow", "somewhat", "slightly", "sometimes",
        "several", "better", "fair", "optimal",
    )
)

# Quantitative concreteness markers: numbers or explicit units of measure.
_QUANT_RE = re.compile(
    r"\d+\s*(%|процент|рубл|монет|мер|голов|килограмм|тонн|литр|дней|лет|год|"
    r"месяц|час|сотня|тысяч|миллион|едениц|единиц|участков|поселени|домов|"
    r"percent|coins|units|days|years|months|hours)",
    re.IGNORECASE,
)
_PLAIN_NUMBER_RE = re.compile(r"\b\d+(\.\d+)?\b")

_MIN_SUBSTANTIVE_WORDS = 5


@dataclass(frozen=True)
class ValidationResult:
    """Verdict of one validation pass over a draft text."""

    ok: bool
    status: DraftStatus
    canonical: CanonicalLaw | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 0..1 — share of imperative/concrete wording in the text (audit info
    # for clients; never a gameplay modifier).
    specificity: float = 0.0

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status.value,
            "canonical": (
                {"kind": self.canonical.kind, "scope": self.canonical.scope,
                 "effect": self.canonical.effect, "refs": list(self.canonical.refs),
                 "text_summary": self.canonical.text_summary}
                if self.canonical is not None else None
            ),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "specificity": round(self.specificity, 4),
        }


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def detect_kind(text: str) -> str | None:
    """Map raw law text onto a canonical kind (the NLP 'formalization' step).

    Deterministic keyword scoring; ties resolve by the table order, so the
    same text always yields the same kind.
    """
    norm = _normalize(text)
    best_kind: str | None = None
    best_score = 0
    for kind, cues in _KIND_CUES.items():
        score = sum(norm.count(cue) for cue in cues)
        if score > best_score:
            best_kind, best_score = kind, score
    return best_kind


def _has_negated_verb_pair(norm: str) -> bool:
    """Direct self-contradiction: two verb phrases sharing a root where one
    is negated — e.g. «собирают налог … не собирают налог», «ограничивает …
    не ограничивает». Implemented as a mirrored-clause scan, not a generic
    semantic engine (the production layer replaces it with a local model).

    Two complementary detectors:
     * clause-level polarity split — a verb root appears both in an
       affirmative and in a negated clause;
     * windowed scan — a negation sits directly before a verb root
       («не собирают») while the same root also occurs unnegated
       elsewhere in the text.
    """
    clauses = [c.strip() for c in re.split(r"[.;,!?]\s*", norm) if c.strip()]
    verbs: dict[str, list[bool]] = {}
    for clause in clauses:
        negated = any(neg in clause for neg in _NEGATIONS)
        for verb in _VERB_ROOTS:
            if verb in clause:
                verbs.setdefault(verb, []).append(negated)
    if any(len({n for n in flags}) > 1 for flags in verbs.values()):
        return True

    # Concept-level detection: an affirmative clause and a negated clause
    # talking about the SAME subject matter (e.g. «зерно облагается налогом»
    # vs «налог на зерно не собирают») contradict each other even when the
    # verbs differ. A concept is keyed by its noun objects plus the verb
    # family it combines with — either side of the pair may carry the verb
    # («облагается …», но «не собирают …»), so families are matched across
    # clauses, not inside one clause.
    def _clause_objects(clause: str) -> set[str]:
        return {o for o in _CONCEPT_OBJECTS if o in clause}

    def _clause_verb_families(clause: str) -> set[str]:
        families: set[str] = set()
        for obj in _CONCEPT_OBJECTS:
            i = clause.find(obj)
            while i != -1:
                window = clause[max(0, i - 30):i + len(obj) + 30]
                families.update(f"{obj}|{root}" for root in _VERB_ROOTS if root in window)
                i = clause.find(obj, i + 1)
        return families

    affirmative_obj: set[str] = set()
    negative_obj: set[str] = set()
    affirmative_fam: set[str] = set()
    negative_fam: set[str] = set()
    for clause in clauses:
        negated = any(neg in clause for neg in _NEGATIONS)
        objs = _clause_objects(clause)
        fams = _clause_verb_families(clause)
        if negated:
            negative_obj |= objs
            negative_fam |= fams
        else:
            affirmative_obj |= objs
            affirmative_fam |= fams
    shared_subjects = negative_obj & affirmative_obj
    shared_families = negative_fam & affirmative_fam
    if shared_families:
        return True
    # Same tax-like subject affirmed in one clause and denied in another.
    return bool(shared_subjects)


# Noun objects that identify which concept a law clause is about; a subject
# appearing both under an affirmation and under a negation is a contradiction.
_CONCEPT_OBJECTS = ("налог", "налога", "налогов", "денег", "деньги", "зерно",
                    "зерна", "лес", "леса", "торг", "торгов", "земл", "раб",
                    "призыв", "льгот", "выплат", "tax", "grain", "money")


_VERB_ROOTS = (
    "соб", "плат", "взыск", "разреш", "запрещ", "огранич", "призыв",
    "освобожда", "гарантиру", "должен",
)


def _specificity(norm: str) -> float:
    """Share of concrete (non-vague) tokens, 0..1. Purely informational."""
    tokens = re.findall(r"[а-яa-zё]+", norm)
    if not tokens:
        return 0.0
    concrete = sum(1 for t in tokens if t not in _VAGUE_WORDS)
    return concrete / len(tokens)


def _is_concrete(norm: str) -> bool:
    """§1: does the text carry enough specifics to generate an effect?

    Concrete iff it contains a quantity (number + unit), an explicit rate,
    or names a bounded subject group («все», «каждый», «никто», thresholds).
    A law made only of vague words produces no effect and is rejected.
    """
    if _QUANT_RE.search(norm) or _PLAIN_NUMBER_RE.search(norm):
        return True
    if any(w in norm for w in ("все", "кажд", "никто", "любой", "ни один",
                               "не менее", "не более", "свыше", "до ",
                               "all ", "every", "no one", "at least")):
        return True
    # An explicit instrument verb plus an object is actionable by itself.
    return any(v in norm for v in _VERB_ROOTS)


class LawValidator:
    """Protocol-compatible local validator.

    Production deployments replace the body with a GPU inference call (a
    sentence encoder running on the server's own video cards); the interface
    below is what every call site (API routes, store hooks) relies on.
    """

    def validate(self, raw_text: str,
                 target_law_id: str | None = None) -> ValidationResult:
        norm = _normalize(raw_text or "")
        errors: list[str] = []
        warnings: list[str] = []

        words = re.findall(r"\S+", norm)
        if len(words) < _MIN_SUBSTANTIVE_WORDS:
            errors.append(
                "Текст слишком короткий: формулировка не может быть "
                "интерпретирована как норма.")

        kind = detect_kind(norm)
        if kind is None:
            errors.append(
                "Не удалось определить тип нормы (налог, запрет, призыв, "
                "льгота, расход, право): добавьте явное предписание.")

        if norm and _has_negated_verb_pair(norm):
            errors.append(
                "Закон внутренне противоречив: одно и то же предписание "
                "утверждается и отрицается в одном тексте.")

        specific = _specificity(norm)
        if norm and words and not _is_concrete(norm):
            errors.append(
                "Формулировка недостаточно конкретна, чтобы порождать "
                "эффект: укажите величины, охват или адресатов нормы.")
        elif norm and specific < 0.5:
            warnings.append(
                "Высокая доля расплывчатых формулировок — закон будет "
                "трудно исполнять.")

        canonical = None
        if not errors and kind is not None:
            scope: dict = {}
            effect: dict = {}
            m = _QUANT_RE.search(norm)
            if m:
                effect["value"] = float(_PLAIN_NUMBER_RE.search(m.group(0)).group(0))
                effect["unit"] = m.group(1).lower()
            if "поселени" in norm or "дерев" in norm or "город" in norm:
                scope["target"] = "settlements"
            if any(h in norm for h in _BAN_HINTS):
                effect["prohibited"] = True
            canonical = CanonicalLaw(
                kind=kind, scope=scope, effect=effect,
                refs=(target_law_id,) if target_law_id else (),
                text_summary=norm[:120],
            )

        status = DraftStatus.VALIDATED if not errors else DraftStatus.INVALID
        return ValidationResult(
            ok=not errors, status=status, canonical=canonical,
            errors=errors, warnings=warnings, specificity=specific)


# Default singleton used by the API layer; swappable for a GPU backend.
DEFAULT_VALIDATOR = LawValidator()
