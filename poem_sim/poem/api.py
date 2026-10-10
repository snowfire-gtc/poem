"""HTTP API layer (§15) — framework-agnostic handlers.

Each handler takes/returns plain JSON-serialisable dicts so it can be wired
into FastAPI/aiohttp later without changes to the domain logic. The handlers
operate against a ``WorldServer`` which owns the law store, chronology and
turn engine (i.e. all state is server-side, §20 rule 1).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from .chronology import Civilization, WorldChronology
from .core import (
    CanonicalLaw,
    CanonicalLawChange,
    DraftStatus,
    EraSource,
    GameSpeed,
    Law,
    LawUserAction,
    SimDuration,
    TimedEffect,
    TimedEffectKind,
)
from .laws import LawStore
from .pricing import cost_of_law_change
from .turn import ACCEPTED_NOTICE, DEFERRED_NOTICE, TurnEngine


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class WorldServer:
    """Composition root for one world."""

    def __init__(
        self,
        world_id: str,
        game_speed: GameSpeed = GameSpeed.STANDARD,
        era_source: EraSource = EraSource.MEDIAN_ACTIVE_CIVILIZATIONS,
    ) -> None:
        self.store = LawStore()
        self.chronology = WorldChronology(world_id, game_speed=game_speed, era_source=era_source)
        self.civilizations: dict[str, Civilization] = {}
        self.timed_effects: list[TimedEffect] = []
        # Published turn reports (§13 step 22); the client fetches the last one.
        self.published_reports: list[dict] = []
        self.engine = TurnEngine(
            world_id, self.store, self.chronology, [], timed_effects=self.timed_effects
        )

    def add_civilization(self, civ: Civilization) -> None:
        self.civilizations[civ.id] = civ
        self.engine.civilizations[civ.id] = civ

    # ------------------------------------------------------------- handlers

    def handle(self, method: str, path: str, body: dict | None = None) -> dict:
        body = body or {}
        try:
            handler = _match_route(method.upper(), _normalize_path(path))
        except ValueError as exc:
            return {"status": 400, "error": str(exc)}
        if handler is None:
            return {"error": f"no route {method} {path}", "status": 404}
        try:
            result = handler(self, path, body)
            return {"status": 200, **result}
        except Exception as exc:  # surface validation errors as 4xx
            return {"status": 400, "error": str(exc)}

    # ================= drafts (§15.1) =====================================

    def create_draft(self, path: str, body: dict) -> dict:
        draft = self.store.create_draft(
            civilization_id=body["civilization_id"],
            raw_text=body["raw_text"],
            target_law_id=body.get("target_law_id"),
            now=_parse_dt(body.get("now")),
        )
        return {"draft": _draft_json(draft)}

    def update_draft(self, path: str, body: dict) -> dict:
        draft_id = _path_id(path, "drafts")
        canonical = None
        if "canonical_draft" in body and body["canonical_draft"] is not None:
            canonical = CanonicalLaw(**body["canonical_draft"])
        draft = self.store.update_draft(
            draft_id,
            raw_text=body.get("raw_text"),
            canonical_draft=canonical,
            status=DraftStatus(body["status"]) if "status" in body else None,
            now=_parse_dt(body.get("now")),
        )
        return {"draft": _draft_json(draft)}

    def get_drafts(self, path: str, body: dict) -> dict:
        parts = [p for p in path.strip("/").split("/") if p]
        if "drafts" in parts and len(parts) > parts.index("drafts") + 1:
            return {"draft": _draft_json(self.store.get_draft(_path_id(path, "drafts")))}
        drafts = self.store.list_drafts(body["civilization_id"])
        return {"drafts": [_draft_json(d) for d in drafts]}

    def delete_draft(self, path: str, body: dict) -> dict:
        draft_id = _path_id(path, "drafts")
        self.store.discard_draft(draft_id)
        return {"discarded": draft_id}

    # ================= revisions (§15.2) ===================================

    def create_revision(self, path: str, body: dict) -> dict:
        law_id = _path_id(path, "laws")
        rev = self.store.record_revision(
            law_id=law_id,
            civilization_id=body["civilization_id"],
            action=LawUserAction(body["action"]),
            raw_text=body["raw_text"],
            canonical_change=CanonicalLawChange(
                action=LawUserAction(body["action"]),
                new_canonical=(
                    CanonicalLaw(**body["canonical_change"]["new_canonical"])
                    if body.get("canonical_change", {}).get("new_canonical") else None
                ),
                suspend_years=body.get("canonical_change", {}).get("suspend_years"),
                urgent=bool(body.get("canonical_change", {}).get("urgent", False)),
                transition=(
                    SimDuration(**body["canonical_change"]["transition"])
                    if body.get("canonical_change", {}).get("transition") else None
                ),
            ),
            now=_parse_dt(body.get("now")),
        )
        notice = ACCEPTED_NOTICE + (
            "\nОна будет учтена в следующем ходе.\nШтрафа за время суток нет."
            if rev.deferred_to_next_turn else ""
        )
        return {"revision": _revision_json(rev), "notice": notice}

    def submit_revision(self, path: str, body: dict) -> dict:
        law_id = _path_id(path, "laws")
        revision_id = _path_id(path, "revisions")
        rev = self.store.get_revision(revision_id)
        if rev.law_id != law_id:
            raise ValueError("revision does not belong to law")
        # "Submit" here = confirm inclusion attempt; cutoff decides the turn.
        return {"revision": _revision_json(rev), "notice": ACCEPTED_NOTICE}

    def cancel_revision(self, path: str, body: dict) -> dict:
        revision_id = _path_id(path, "revisions")
        rev = self.store.cancel_pending_change(revision_id)
        return {"revision": _revision_json(rev)}

    def get_revisions(self, path: str, body: dict) -> dict:
        law_id = _path_id(path, "laws")
        return {
            "revisions": [_revision_json(r) for r in self.store.version_history(law_id)]
        }

    # ================= pending (§15.3) =====================================

    def get_pending(self, path: str, body: dict) -> dict:
        """Shows which edits join which slice, superseded/rejected/deferred lists."""
        from .core import LawRevisionStatus as LRS

        turn = self.engine.current_turn
        cutoff = turn.simulation_cutoff_at if turn else None
        included, deferred, superseded, rejected = [], [], [], []
        for rev in self.store._revisions.values():
            payload = _revision_json(rev)
            if rev.status == LRS.PENDING:
                if cutoff is not None and rev.received_at > cutoff:
                    deferred.append(payload)      # будет учтена в следующем ходу
                else:
                    included.append(payload)      # войдёт в текущий снимок
            elif rev.status == LRS.SUPERSEDED:
                superseded.append(payload)        # перезаписана более поздней
            elif rev.status == LRS.REJECTED:
                rejected.append(payload)
            else:
                included.append(payload)          # IncludedInSimulation / Applied
        return {
            "will_apply_this_slice": included,
            "awaiting_next_turn": deferred,
            "superseded": superseded,
            "rejected": rejected,
            "cutoff": cutoff.isoformat() if cutoff else None,
            "note": "Время подачи правки не влияет на эффективность (TASK-060).",
        }

    # ================= cost preview (§16: "показывать стоимость") ==========

    def preview_cost(self, path: str, body: dict) -> dict:
        """Estimate the §5.1 price of a *not yet submitted* edit.

        Uses exactly the same inputs as snapshot pricing (law state, action
        weight, scope volume, prospective canonical conflicts, transition,
        urgency), so the number shown to the player before submission is the
        number charged at the cutoff — no drift, and no wall-clock term ever
        (§20.5).
        """
        law_id = _path_id(path, "laws")
        law = self.store.get_law(law_id)
        action = LawUserAction(body["action"])
        new_canonical = None
        if body.get("new_canonical") is not None:
            new_canonical = CanonicalLaw(**body["new_canonical"])
        transition_years = float(body.get("transition_years", 0.0))
        urgent = bool(body.get("urgent", False))
        scope_delta = len(body.get("raw_text", "")) / 500.0

        contradictions = self._count_preview_conflicts(
            law, new_canonical or law.canonical)
        cost = cost_of_law_change(
            law, action, cfg=self.engine.pricing_cfg,
            scope_delta=scope_delta,
            contradictions=contradictions,
            transition_years=transition_years,
            urgent=urgent,
        )
        return {
            "estimated_cost": cost,
            "contradictions": contradictions,
            "breakdown_available": True,
            "note": "Стоимость не зависит от времени подачи правки.",

        }

    def _count_preview_conflicts(self, law, canon) -> int:
        """Conflict count against the current active set (no turn running)."""
        from .core import kinds_conflict

        if canon is None:
            return 0
        count = 0
        for other in self.store.laws_of(law.civilization_id):
            if other.id == law.id or other.canonical is None:
                continue
            if other.status.value not in ("active", "in_transition"):
                continue
            if other.canonical.kind == canon.kind or \
                    kinds_conflict(canon.kind, other.canonical.kind):
                count += 1
        return count

    # ================= chronology (§15.4) ===================================

    def get_chronology(self, path: str, body: dict) -> dict:
        from .chronology import calculate_turn_span

        ch = self.chronology
        next_span = calculate_turn_span(ch, list(self.civilizations.values()))
        return {
            "current_turn": ch.current_turn,
            "current_year": ch.current_year,
            "current_season": ch.current_season.value,
            "current_era": ch.current_era.value,
            "next_turn_span": {
                "years": next_span.years,
                "seasons": next_span.seasons,
                "days": next_span.days,
            },
        }

    # ================= turns / laws (support) ===============================

    def open_turn(self, path: str, body: dict) -> dict:
        turn = self.engine.open_turn(
            started_at=_parse_dt(body["started_at"]),
            simulation_cutoff_at=_parse_dt(body["simulation_cutoff_at"]),
            seed=body.get("seed", 0),
        )
        return {"turn_id": turn.id, "turn_span_years": turn.turn_span.years}

    def run_turn(self, path: str, body: dict) -> dict:
        report = self.engine.run_turn()
        # Publish the turn report (§13 step 22): raw numbers plus the
        # generated client view and queued illustration tasks.
        result = {
            "turn_number": report.turn_number,
            "years_elapsed": report.years_elapsed,
            "era_before": report.era_before.value,
            "era_after": report.era_after.value,
            "world_year_before": report.world_year_before,
            "world_year_after": report.world_year_after,
            "applied_revisions": report.applied_revisions,
            "deferred_revisions": report.deferred_revisions,
            "superseded_revisions": report.superseded_revisions,
            "expired_effects": report.expired_effects,
            "economy": report.economy,
            "notices": report.notices,
            "admin_costs": report.admin_costs,
            "view": report.view,
            "image_tasks": report.image_tasks,
        }
        self.published_reports.append(result)
        return result

    def get_turn_report(self, path: str, body: dict) -> dict:
        """GET /api/v1/worlds/{id}/turns/report — last published report (§16)."""
        if not self.published_reports:
            return {"report": None}
        return {"report": self.published_reports[-1]}

    def list_laws(self, path: str, body: dict) -> dict:
        civ = body.get("civilization_id")
        laws = self.store.laws_of(civ) if civ else list(self.store._laws.values())
        return {
            "laws": [
                {
                    "id": l.id, "title": l.title, "status": l.status.value,
                    "tradition": l.tradition, "age_years": l.age_years,
                    "raw_text": l.raw_text,
                }
                for l in laws
            ]
        }

    def create_law(self, path: str, body: dict) -> dict:
        import uuid as _uuid

        law = Law(
            id=body.get("id") or str(_uuid.uuid4()),
            civilization_id=body["civilization_id"],
            title=body["title"],
            raw_text=body["raw_text"],
            canonical=CanonicalLaw(**body["canonical"]) if body.get("canonical") else None,
            complexity=body.get("complexity", 1.0),
            administrative_load=body.get("administrative_load", 1.0),
        )
        self.store.add_law(law)
        return {"law": {"id": law.id, "title": law.title, "status": law.status.value},
                "law_id": law.id}

    def add_treaty(self, path: str, body: dict) -> dict:
        eff = Treaty_as_effect(body)
        self.timed_effects.append(eff)
        self.engine.timed_effects = self.timed_effects
        return {"treaty_id": eff.id, "duration_years": eff.remaining.years}

    _ROUTES: dict = {}


def Treaty_as_effect(body: dict) -> TimedEffect:
    import uuid

    return TimedEffect(
        id=body.get("id") or str(uuid.uuid4()),
        world_id=body["world_id"],
        civilization_id=body.get("civilization_id"),
        kind=TimedEffectKind.TREATY,
        remaining=SimDuration(years=float(body["duration_years"])),
        payload={"treaty_kind": body.get("kind", "peace")},
    )


def _normalize_path(path: str) -> str:
    """Collapse id segments so routes can be matched generically."""
    parts = [p for p in path.strip("/").split("/") if p]
    out = []
    for i, p in enumerate(parts):
        out.append("{id}" if _looks_like_id(p, parts, i) else p)
    return "/" + "/".join(out)


def _looks_like_id(token: str, parts: list[str], index: int) -> bool:
    known_ids = {"laws", "drafts", "revisions", "worlds", "chronology", "pending",
                 "turns", "report", "api", "v1", "submit", "cancel", "treaties",
                 "civilizations", "cost_preview"}
    if token in known_ids:
        return False
    prev = parts[index - 1] if index else ""
    return prev in {"laws", "drafts", "revisions", "worlds", "treaties"}


def _path_id(path: str, collection: str) -> str:
    """Extract the id that follows ``collection`` in a concrete request path.

    Handlers receive the *original* path (not the normalised one), so ids
    must be located by their preceding collection segment — positional
    indexing breaks for paths like ``/api/v1/laws/drafts/{id}``, where the
    id is at index 4, not 3.
    """
    parts = [p for p in path.strip("/").split("/") if p]
    try:
        i = parts.index(collection)
    except ValueError:
        raise ValueError(f"malformed path: {path!r}") from None
    if i + 1 >= len(parts):
        raise ValueError(f"missing {collection} id in path: {path!r}")
    return parts[i + 1]


def _draft_json(d) -> dict:
    return {
        "id": d.id,
        "civilization_id": d.civilization_id,
        "target_law_id": d.target_law_id,
        "raw_text": d.raw_text,
        "status": d.status.value,
        "updated_at": d.updated_at.isoformat(),
    }


def _revision_json(r) -> dict:
    return {
        "id": r.id,
        "law_id": r.law_id,
        "civilization_id": r.civilization_id,
        "action": r.action.value,
        "raw_text": r.raw_text,
        "sequence_number": r.sequence_number,
        "received_at": r.received_at.isoformat(),
        "simulation_turn_id": r.simulation_turn_id,
        "status": r.status.value,
        "cost": r.cost,
        "deferred_to_next_turn": r.deferred_to_next_turn,
    }


# Register the REST table of §15.
import re  # noqa: E402

# NOTE: patterns are matched against the *leading slash* normalised path
# (``_normalize_path`` always returns "/..."), so every anchor must start
# with "^/". Previously they started with "^api/..." and no route ever
# matched, making the whole API return 404.
_ROUTE_TABLE = [
    ("POST", r"^/api/v1/laws/drafts$", WorldServer.create_draft),
    ("PUT", r"^/api/v1/laws/drafts/\{id\}$", WorldServer.update_draft),
    ("GET", r"^/api/v1/laws/drafts$", WorldServer.get_drafts),
    ("GET", r"^/api/v1/laws/drafts/\{id\}$", WorldServer.get_drafts),
    ("DELETE", r"^/api/v1/laws/drafts/\{id\}$", WorldServer.delete_draft),
    ("POST", r"^/api/v1/laws/\{id\}/revisions$", WorldServer.create_revision),
    ("POST", r"^/api/v1/laws/\{id\}/cost_preview$", WorldServer.preview_cost),
    ("GET", r"^/api/v1/laws/\{id\}/revisions$", WorldServer.get_revisions),
    ("POST", r"^/api/v1/laws/\{id\}/revisions/\{id\}/submit$", WorldServer.submit_revision),
    ("POST", r"^/api/v1/laws/\{id\}/revisions/\{id\}/cancel$", WorldServer.cancel_revision),
    ("GET", r"^/api/v1/laws/pending$", WorldServer.get_pending),
    ("GET", r"^/api/v1/worlds/\{id\}/chronology$", WorldServer.get_chronology),
    ("POST", r"^/api/v1/worlds/\{id\}/turns/open$", WorldServer.open_turn),
    ("POST", r"^/api/v1/worlds/\{id\}/turns/run$", WorldServer.run_turn),
    ("GET", r"^/api/v1/worlds/\{id\}/turns/report$", WorldServer.get_turn_report),
    ("GET", r"^/api/v1/laws$", WorldServer.list_laws),
    ("POST", r"^/api/v1/laws$", WorldServer.create_law),
    ("POST", r"^/api/v1/treaties$", WorldServer.add_treaty),
]

WorldServer._ROUTES = {
    (m, re.compile(p)): h for m, p, h in _ROUTE_TABLE
}


def _match_route(method: str, norm_path: str):
    for (m, pattern), handler in WorldServer._ROUTES.items():
        if m == method and pattern.match(norm_path):
            return handler
    return None
