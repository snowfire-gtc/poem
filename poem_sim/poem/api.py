"""HTTP API layer (§15) — framework-agnostic handlers.

Each handler takes/returns plain JSON-serialisable dicts so it can be wired
into FastAPI/aiohttp later without changes to the domain logic. The handlers
operate against a ``WorldServer`` which owns the law store, chronology and
turn engine (i.e. all state is server-side, §20 rule 1).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .chronology import Civilization, WorldChronology
from .core import (
    CanonicalLaw,
    CanonicalLawChange,
    DraftStatus,
    EraSource,
    GameSpeed,
    Law,
    LawRevisionStatus,
    LawStatus,
    LawUserAction,
    SimDuration,
    SeasonId,
    TimedEffect,
    TimedEffectKind,
    War,
)
from .laws import LawStore
from .pricing import cost_of_law_change
from .turn import ACCEPTED_NOTICE, DEFERRED_NOTICE, TurnEngine

# Local GPU renderers renew this lease while a diffusion job runs; expired
# leases (crashed worker) release the task back to the queue (§13 step 20).
IMAGE_LEASE_SECONDS_DEFAULT = 600.0

# ---------------------------------------------------------------------------
# Server administration authentication (§15 security addendum)
# ---------------------------------------------------------------------------
# Mutating world-administration routes — everything that changes laws, wars,
# treaties, turns or civilization setup — require an admin credential. The
# credential is compared in constant time (hmac.compare_digest) so neither a
# wrong length nor a wrong prefix leaks through response timing.
ADMIN_TOKEN_ENV = "POEM_ADMIN_TOKEN"
WORKER_TOKEN_ENV = "POEM_IMAGE_WORKER_TOKEN"
AUTH_HEADER = "X-Auth-Token"
ADMIN_SCHEME = "Bearer "


def generate_admin_token() -> str:
    """Mint a cryptographically strong admin token (URL-safe, 256 bits)."""
    return secrets.token_urlsafe(32)


class AuthError(Exception):
    """Authentication/authorization failure carrying an HTTP status code."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


def _extract_credential(headers: dict | None, body: dict) -> tuple[str | None, str | None]:
    """Pull (admin_token, worker_token) out of request headers/body.

    Accepted spellings (first hit wins):
      * ``Authorization: Bearer <token>`` header,
      * ``X-Auth-Token: <token>`` header,
      * ``{"auth_token": ...}`` inside the JSON body (frameworks that only
        forward method/path/body),
      * ``{"worker_token": ...}`` / ``X-Worker-Token`` for GPU workers.
    """
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    admin = headers.get("x-auth-token") or headers.get("x-admin-token")
    if not admin:
        authz = headers.get("authorization", "")
        if isinstance(authz, str) and authz.startswith(ADMIN_SCHEME):
            admin = authz[len(ADMIN_SCHEME):].strip()
    if not admin:
        admin = body.get("auth_token") or body.get("admin_token")
    worker = (headers.get("x-worker-token") or body.get("worker_token"))
    return (str(admin) if admin else None,
            str(worker) if worker else None)

# Hard cap for one rendered illustration stored through the API (§20.4:
# assets live on the server's own disk, never on external object storage).
ASSET_MAX_BYTES_DEFAULT = 32 * 1024 * 1024


def _store_asset_bytes(asset_dir: Path | None, asset_ref: str, data: bytes) -> dict:
    """Persist a locally rendered image under ``asset_dir`` and return meta.

    ``asset_ref`` is treated as a relative path inside the server-owned
    asset directory: it is sanitised (no absolute paths, no "..", only
    [A-Za-z0-9._/-]) so a renderer can never write outside the sandbox —
    images come from the server's own GPUs, but the reference still passes
    through HTTP and must be validated like any other client input (§20.4).
    """
    if asset_dir is None:
        raise ValueError("asset storage is not configured for this world server")
    ref = str(asset_ref).strip().lstrip("/")
    if not ref or any(c in ref for c in "\\:*?\"<>|") or "\x00" in ref:
        raise ValueError(f"invalid asset_ref: {asset_ref!r}")
    parts = ref.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"invalid asset_ref: {asset_ref!r}")
    root = Path(asset_dir).resolve()
    target = (root / ref).resolve()
    if not str(target).startswith(str(root) + "/"):
        raise ValueError(f"asset_ref escapes the asset directory: {asset_ref!r}")
    if len(data) == 0:
        raise ValueError("empty asset payload")
    if len(data) > ASSET_MAX_BYTES_DEFAULT:
        raise ValueError(f"asset too large: {len(data)} bytes")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)          # atomic-ish: readers never see half files
    tmp.replace(target)
    media_type = mimetypes.guess_type(ref)[0] or "application/octet-stream"
    return {
        "asset_ref": ref,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "media_type": media_type,
    }


def _expire_image_leases(queue: list) -> None:
    """Return running tasks whose lease has expired back to ``queued``."""
    now = time.time()
    for t in queue:
        if (t.status == "running" and t.lease_expires_at is not None
                and t.lease_expires_at <= now):
            t.status = "queued"
            t.claimed_by = None
            t.lease_expires_at = None


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _received_at(body: dict) -> datetime | None:
    """Audit timestamp for a revision (§3.4 ``received_at``).

    Accepts both spellings used by clients/tests — ``now`` and the canonical
    ``received_at`` — so a supplied submission time is never silently ignored.
    It is stored for audit only and never modifies cost or effectiveness
    (TASK-060, §20.4).
    """
    return _parse_dt(body.get("now") or body.get("received_at"))


class WorldServer:
    """Composition root for one world."""

    def __init__(
        self,
        world_id: str,
        game_speed: GameSpeed = GameSpeed.STANDARD,
        era_source: EraSource = EraSource.MEDIAN_ACTIVE_CIVILIZATIONS,
        season: SeasonId | None = None,
        asset_dir: str | Path | None = None,
        validator=None,
        admin_token: str | None = None,
        worker_token: str | None = None,
    ) -> None:
        # ``validator`` (TASK-066): pluggable server-side neural layer for
        # law-text validation; defaults to the deterministic local heuristic.
        self.store = LawStore(validator=validator)
        # Server administration credential (§15 security addendum). Read
        # from POEM_ADMIN_TOKEN when not passed explicitly; empty/absent
        # means "open server" (tests & local play only — startup helpers
        # should refuse to serve mutating routes without it in production).
        self.admin_token = admin_token if admin_token is not None \
            else os.environ.get(ADMIN_TOKEN_ENV, "")
        # Separate, weaker credential for GPU render workers so they can
        # claim/heartbeat/complete image tasks without full admin rights.
        self.worker_token = worker_token if worker_token is not None \
            else os.environ.get(WORKER_TOKEN_ENV, "")
        self.chronology = WorldChronology(world_id, game_speed=game_speed, era_source=era_source)
        if season is not None:
            # Setting-specific calendar (alien planet, §7.2): e.g. a world
            # whose year begins with LONG_NIGHT (§15.4 sample response).
            self.chronology.current_season = season
        self.civilizations: dict[str, Civilization] = {}
        self.timed_effects: list[TimedEffect] = []
        # Server-owned directory for illustrations produced by local GPU
        # renderers (§20.4: assets never leave the server's own storage).
        self.asset_dir = Path(asset_dir) if asset_dir is not None else None
        # Published turn reports (§13 step 22); the client fetches the last one.
        self.published_reports: list[dict] = []
        self.engine = TurnEngine(
            world_id, self.store, self.chronology, [], timed_effects=self.timed_effects
        )
        # The engine rebinds its effects list when a turn finalises (it
        # replaces the list with survivors), so the server must always read
        # the *live* engine list — otherwise newly created transitions /
        # suspensions would be invisible to API queries.
        self._effects_view = lambda: self.engine.timed_effects

    def add_civilization(self, civ: Civilization) -> None:
        self.civilizations[civ.id] = civ
        self.engine.civilizations[civ.id] = civ

    # ================= civilizations (setup / §9 era source) ================

    def create_civilization(self, path: str, body: dict) -> dict:
        """POST /api/v1/worlds/{id}/civilizations — register a civilization.

        Civilizations drive era determination (§9): their ``progress``
        decides the world era and therefore the Civilization-like turn
        interval (§6.2). Without this route a purely HTTP client could not
        populate the world at all (era sources like
        ``weighted_average`` need population, research needs rates), so
        setup must be reachable through the API too.
        """
        civ = Civilization(
            id=body["id"],
            name=body.get("name", ""),
            progress=float(body.get("progress", 0.0)),
            active=bool(body.get("active", True)),
            population=float(body.get("population", 1000.0)),
            annual_production_rate=float(body.get("annual_production_rate", 0.0)),
            annual_growth_rate=float(body.get("annual_growth_rate", 0.005)),
            annual_research_rate=float(body.get("annual_research_rate", 0.0)),
            culture_modifier=float(body.get("culture_modifier", 1.0)),
            technology_modifier=float(body.get("technology_modifier", 1.0)),
            administrative_budget=float(body.get("administrative_budget", 0.0)),
            research_to_progress=float(body.get("research_to_progress", 1e-4)),
        )
        if civ.id in self.civilizations:
            raise ValueError(f"civilization {civ.id} already exists")
        self.add_civilization(civ)
        # TASK-068: every new civilization gets its koine registered in the
        # language tree immediately (before any turn runs), so settlements,
        # pricing and reports can consult it right away.
        self.engine.language.add_civilization(
            civ, self.chronology.current_era, self.chronology.current_year)
        return {"civilization": {
            "id": civ.id, "name": civ.name, "progress": civ.progress,
            "active": civ.active, "population": civ.population,
            "language_unity": round(civ.language_unity, 4),
        }}

    # ================= settlements & language (TASK-068) ====================

    def add_settlement(self, path: str, body: dict) -> dict:
        """POST /api/v1/worlds/{id}/settlements — found or annex a seat.

        A settlement is the carrier of local speech (§ language design):
        terrain decides how isolated it is, infrastructure (roads/harbours)
        decides how far the koine reaches.  Mountain/island foundations are
        seeded with latent divergence — they may branch off into their own
        dialect within a few turns unless the ruler builds roads.
        """
        from .core import Settlement, Terrain
        civ_id = body.get("civilization_id") or body.get("civ_id")
        if not civ_id:
            raise ValueError("civilization_id (or civ_id) is required")
        sid = body.get("id")
        if not sid:
            raise ValueError("settlement id is required")
        if any(s.id == sid for s in self.engine.language.settlements):
            raise ValueError(f"settlement {sid} already exists")
        terrain_raw = body.get("terrain", "plain")
        try:
            terrain = Terrain(terrain_raw) if isinstance(terrain_raw, str) \
                else terrain_raw
        except ValueError:
            raise ValueError(f"unknown terrain {terrain_raw!r}; "
                             f"expected one of {[t.value for t in Terrain]}")
        s = Settlement(
            id=sid,
            civilization_id=civ_id,
            name=body.get("name", sid),
            terrain=terrain,
            pop=float(body.get("pop", 1000.0)),
            infrastructure=min(max(float(body.get("infrastructure", 0.0)), 0.0), 1.0),
        )
        d = self.engine.language.add_settlement(
            s, self.chronology.current_era, self.chronology.current_year)
        return {"settlement": {
            "id": s.id, "name": s.name, "civilization_id": s.civilization_id,
            "terrain": s.terrain.value, "pop": s.pop,
            "infrastructure": s.infrastructure,
            "dialect_id": d.id, "divergence": round(d.divergence, 4),
        }}

    def get_settlements(self, path: str, body: dict) -> dict:
        """GET /api/v1/worlds/{id}/settlements — list seats with their speech."""
        out = []
        for s in self.engine.language.settlements:
            d = self.engine.language.dialect_of(s)
            out.append({
                "id": s.id, "name": s.name, "civilization_id": s.civilization_id,
                "terrain": s.terrain.value, "pop": s.pop,
                "infrastructure": round(s.infrastructure, 4),
                "dialect_id": s.dialect_id,
                "divergence": round(d.divergence, 4) if d else 0.0,
            })
        return {"settlements": out}

    def get_language_census(self, path: str, body: dict) -> dict:
        """GET /api/v1/worlds/{id}/language[/{civ_id}] — the dialect tree.

        Without ``civ_id`` returns the census of every active civilization;
        the projection matches ``LanguageState.census`` (unity, dialect list
        with speaker counts, settlement assignments).
        """
        parts = [p for p in path.strip("/").split("/") if p]
        civ_ids = ([parts[-1]] if "language" in parts
                   and len(parts) > parts.index("language") + 1
                   else list(self.civilizations))
        result = {}
        for cid in civ_ids:
            cen = self.engine.language.census(cid)
            if cen:
                result[cid] = cen
        return {"language": result}

    # ------------------------------------------------------------- handlers

    def _check_admin(self, token: str | None) -> None:
        """Constant-time admin credential check (raises AuthError)."""
        expected = self.admin_token or ""
        if not expected:
            return                      # open server (tests / local play)
        if not token:
            raise AuthError("authentication required: provide an admin "
                            f"token via '{AUTH_HEADER}' header or "
                            "'Authorization: Bearer <token>'", status=401)
        if not hmac.compare_digest(str(token), str(expected)):
            raise AuthError("invalid admin credentials", status=403)

    def _check_worker_or_admin(self, worker: str | None,
                               admin: str | None) -> None:
        """GPU render endpoints accept either the dedicated worker token
        or the full admin token; anything else is rejected."""
        if self.worker_token and worker and \
                hmac.compare_digest(str(worker), str(self.worker_token)):
            return
        self._check_admin(admin)

    def handle(self, method: str, path: str, body: dict | None = None,
               headers: dict | None = None) -> dict:
        body = body or {}
        # Query strings are part of REST routes (GET /laws?civilization_id=c1,
        # guide_client.md §5.3): strip them before matching and fold the pairs
        # into the handler input so both HTTP and library hosts behave alike.
        if "?" in path:
            path, _, query = path.partition("?")
            from urllib.parse import parse_qsl
            for key, value in parse_qsl(query):
                body.setdefault(key, value)
        try:
            norm = _normalize_path(path)
            handler = _match_route(method.upper(), norm)
        except ValueError as exc:
            return {"status": 400, "error": str(exc)}
        if handler is None:
            return {"error": f"no route {method} {path}", "status": 404}
        # §15 security addendum: mutating routes are world administration.
        # Read-only GETs stay public so the game client can poll state.
        if method.upper() in {"POST", "PUT", "DELETE", "PATCH"}:
            admin_tok, worker_tok = _extract_credential(headers, body)
            try:
                if handler in _WORKER_ROUTES:
                    self._check_worker_or_admin(worker_tok, admin_tok)
                else:
                    self._check_admin(admin_tok)
            except AuthError as exc:
                return {"status": exc.status, "error": str(exc)}
        try:
            result = handler(self, path, body)
            return {"status": 200, **result}
        except Exception as exc:  # surface validation errors as 4xx
            return {"status": 400, "error": str(exc)}

    # ================= drafts (§15.1) =====================================

    def create_draft(self, path: str, body: dict) -> dict:
        # Accept both canonical field names and short aliases (civ_id/title/text)
        # so thin clients can post human-friendly payloads.
        civ = body.get("civilization_id") or body.get("civ_id")
        text = body.get("raw_text") or body.get("text")
        if not civ or not text:
            raise ValueError("civilization_id (or civ_id) and raw_text (or text) are required")
        title = body.get("title")
        if title and f"about:{title}" not in text:
            text = f"[{title}] {text}"
        draft = self.store.create_draft(
            civilization_id=civ,
            raw_text=text,
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

    def validate_draft(self, path: str, body: dict) -> dict:
        """POST /api/v1/laws/drafts/{id}/validate (§3.3, TASK-066).

        Runs the server-side neural layer over the draft's raw text and
        returns the verdict together with the updated draft. A failing
        verdict leaves the draft on the server in status ``Invalid`` so it
        stays editable from any device; the verdict never depends on the
        time of day (§2).
        """
        draft_id = _path_id(path, "drafts")
        draft = self.store.validate_draft(draft_id)
        result = self.store.validator.validate(
            draft.raw_text, draft.target_law_id)
        return {"draft": _draft_json(draft), "validation": result.to_json()}

    # ================= revisions (§15.2) ===================================

    def create_revision(self, path: str, body: dict) -> dict:
        law_id = _path_id(path, "laws")
        action = _parse_action(body["action"])
        rev = self.store.record_revision(
            law_id=law_id,
            civilization_id=body["civilization_id"],
            action=action,
            raw_text=body["raw_text"],
            canonical_change=CanonicalLawChange(
                action=action,
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
            now=_received_at(body),
        )
        notice = ACCEPTED_NOTICE + (
            "\nОна будет учтена в следующем ходе.\nШтрафа за время суток нет."
            if rev.deferred_to_next_turn else ""
        )
        return {"revision": _revision_json(rev), "notice": notice}

    def submit_revision(self, path: str, body: dict) -> dict:
        """POST /api/v1/laws/{id}/revisions/{revision_id}/submit (§15.2).

        Three roles, mirroring §3.2/§3.3 (``LawUserAction.SubmitDraft``):

        * ``{"draft_id": ...}`` in the body — convert a server-side draft
          into an official revision (validated drafts only, §3.3); the
          created revision is Pending and its turn is decided by the
          logical cutoff alone (§4.1), never by time of day (§2).
        * ``{"reject_reason": ...}`` — validation verdict Rejected (§3.4):
          the revision is removed from future slices, kept in history for
          audit (§16.2 version list) and reported via GET /laws/pending
          (§15.3 «какие отклонены»).
        * otherwise — confirm an already-recorded revision: it stays
          Pending; the cutoff decides whether it joins this or the next
          slice (§4.4). No penalty either way.
        """
        law_id = _path_id(path, "laws")
        revision_id = _path_id(path, "revisions")
        draft_id = body.get("draft_id")
        reject_reason = body.get("reject_reason")
        if draft_id is not None:
            rev = self.store.submit_draft(draft_id, now=_parse_dt(body.get("now")))
            if rev.law_id != law_id:
                raise ValueError("draft targets another law")
        elif reject_reason is not None:
            rev = self.store.get_revision(revision_id)
            if rev.law_id != law_id:
                raise ValueError("revision does not belong to law")
            if rev.status.value != "Pending":
                raise ValueError(
                    f"only Pending revisions can be rejected "
                    f"(current status: {rev.status.value})")
            self.store.mark_rejected(revision_id, reason=str(reject_reason))
            return {
                "revision": _revision_json(self.store.get_revision(revision_id)),
                "notice": (
                    "Правка отклонена при валидации.\n"
                    "Она сохранена в истории версий для аудита и не будет "
                    "учтена ни в одном ходе."
                ),
            }
        else:
            rev = self.store.get_revision(revision_id)
            if rev.law_id != law_id:
                raise ValueError("revision does not belong to law")
        # "Submit" here = confirm inclusion attempt; cutoff decides the turn.
        return {
            "revision": _revision_json(rev),
            "notice": ACCEPTED_NOTICE + (
                "\nОна будет учтена в следующем ходе.\nШтрафа за время суток нет."
                if rev.deferred_to_next_turn else ""
            ),
        }

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
        """§15.3 — five buckets required by the design doc:

         * what will be counted in the NEXT slice (pending, before cutoff);
         * what has ALREADY joined the current snapshot (IncludedInSimulation);
         * what was SUPERSEDED by a later confirmed version;
         * what was REJECTED;
         * what AWAITS the next turn (received after the cutoff — preserved,
           never discarded, no time-of-day penalty, §4.4).
        """
        from .core import LawRevisionStatus as LRS

        turn = self.engine.current_turn
        cutoff = turn.simulation_cutoff_at if turn else None
        will_apply, in_snapshot, deferred, superseded, rejected, failed = \
            [], [], [], [], [], []
        for rev in self.store._revisions.values():
            payload = _revision_json(rev)
            if rev.status == LRS.PENDING:
                if cutoff is not None and rev.received_at > cutoff:
                    deferred.append(payload)      # будет учтена в следующем ходу
                else:
                    will_apply.append(payload)    # войдёт в текущий снимок
            elif rev.status == LRS.INCLUDED_IN_SIMULATION:
                in_snapshot.append(payload)       # уже вошла в снимок этого хода
            elif rev.status == LRS.SUPERSEDED:
                superseded.append(payload)        # перезаписана более поздней
            elif rev.status == LRS.REJECTED:
                rejected.append(payload)
            elif rev.status == LRS.FAILED:
                # §3.4 Failed — applied-attempt raised during simulation;
                # kept visible so the player learns the verdict (§15.3).
                failed.append(payload)
            # APPLIED revisions are already part of the law's official
            # version history (GET /api/v1/laws/{id}); they are neither
            # pending nor "in this turn's snapshot" anymore (§3.4 statuses).
        return {
            "will_apply_this_slice": will_apply,
            "included_in_current_snapshot": in_snapshot,
            "awaiting_next_turn": deferred,
            "superseded": superseded,
            "rejected": rejected,
            "failed": failed,
            "cutoff": cutoff.isoformat() if cutoff else None,
            "note": "Время подачи правки не влияет на эффективность (TASK-060).",
        }

    def get_law_status(self, path: str, body: dict) -> dict:
        """§16.3 — per-law edit status feed for the web client.

        Answers exactly what the design doc requires to be shown next to an
        edit ("Правка сохранена… Время подачи не влияет на эффективность"),
        categorised against the *current turn's* logical cutoff (§4.1):

         * included_in_current_snapshot — already joined this turn's slice;
         * will_apply_this_slice        — confirmed before the cutoff;
         * awaiting_next_turn           — made after the cutoff (preserved,
           no penalty, §4.4);
         * superseded                   — overwritten by a later version;
         * rejected / failed            — server verdicts (§3.4).

        Time of day is never part of any bucket or notice (§20.4).
        """
        from .core import LawRevisionStatus as LRS

        law_id = _path_id(path, "laws")
        law = self.store.get_law(law_id)          # raises -> 400 via handle()

        turn = self.engine.current_turn
        cutoff = turn.simulation_cutoff_at if turn else None
        buckets: dict[str, list[dict]] = {
            "included_in_current_snapshot": [],
            "will_apply_this_slice": [],
            "awaiting_next_turn": [],
            "superseded": [],
            "rejected": [],
            "failed": [],
        }
        for rev in self.store.version_history(law_id):
            payload = _revision_json(rev)
            if rev.status == LRS.PENDING:
                if cutoff is not None and rev.received_at > cutoff:
                    buckets["awaiting_next_turn"].append(payload)
                else:
                    buckets["will_apply_this_slice"].append(payload)
            elif rev.status == LRS.INCLUDED_IN_SIMULATION:
                buckets["included_in_current_snapshot"].append(payload)
            elif rev.status == LRS.SUPERSEDED:
                buckets["superseded"].append(payload)
            elif rev.status == LRS.REJECTED:
                buckets["rejected"].append(payload)
            elif rev.status == LRS.FAILED:
                buckets["failed"].append(payload)
            # APPLIED revisions are the official version history of the law
            # itself (GET /api/v1/laws/{id}, §16.2), not a pending edit.

        notices: list[str] = []
        if buckets["will_apply_this_slice"] or buckets["included_in_current_snapshot"]:
            notices.append(ACCEPTED_NOTICE)
        if buckets["awaiting_next_turn"]:
            notices.append(DEFERRED_NOTICE)

        return {
            "law_id": law_id,
            "law_status": law.status.value,
            "turn_open": turn is not None,
            "cutoff": cutoff.isoformat() if cutoff else None,
            **buckets,
            "notices": notices,
            "note": "Время подачи правки не влияет на эффективность (TASK-060).",
        }

    def get_snapshot(self, path: str, body: dict) -> dict:
        """§4.3 / §15.3 — the exact TurnLawSnapshot the simulation reads.

        Readable at any time; reflects the last prepared snapshot (statuses
        IncludedInSimulation correspond to what has already joined it)."""
        snaps = self.engine.last_snapshots
        return {
            "turn_id": snaps[0].turn_id if snaps else None,
            "civilizations": [
                {
                    "civilization_id": s.civilization_id,
                    "active_laws": [l.id for l in s.active_laws],
                    "pending_amendments": [r.id for r in s.pending_amendments],
                    "pending_repeals": [r.id for r in s.pending_repeals],
                    "suspended_laws": list(s.suspended_laws),
                    "customary_remnants": [c.id for c in s.customary_remnants],
                    "conflicts": [
                        {"law_a": c.law_a, "law_b": c.law_b,
                         "folklore_ref": c.folklore_ref,
                         "revision_id": c.revision_id,
                         "severity": c.severity,
                         "description": c.description}
                        for c in s.conflicts
                    ],
                }
                for s in snaps
            ],
            "note": "Симуляция читает именно этот снимок (§4.3).",
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
        action = _parse_action(body["action"])
        new_canonical = None
        if body.get("new_canonical") is not None:
            new_canonical = CanonicalLaw(**body["new_canonical"])
        transition_years = float(body.get("transition_years", 0.0))
        urgent = bool(body.get("urgent", False))
        scope_delta = len(body.get("raw_text", "")) / 500.0

        contradictions = self._count_preview_conflicts(
            law, new_canonical or law.canonical, action=action)
        # §5 «уровень бюрократии»: same input as snapshot pricing — the civ's
        # FINAL active norm set. The engine prices amendments against the
        # post-repeal configuration (§13 step 13), so a prospective REPEAL
        # removes its own law from the bureaucratic load already at preview
        # time: after repeal the law is no longer administered. Without this
        # exclusion preview and charge drift by one bureaucracy step.
        active_laws = [
            l for l in self.store.laws_of(law.civilization_id)
            if l.status.value in ("active", "in_transition")
            and not (action == LawUserAction.REPEAL_LAW and l.id == law.id)
        ]
        cost = cost_of_law_change(
            law, action, cfg=self.engine.pricing_cfg,
            scope_delta=scope_delta,
            contradictions=contradictions,
            transition_years=transition_years,
            urgent=urgent,
            active_laws=active_laws,
            # TASK-068: same language term as snapshot pricing — preview and
            # charge stay identical (both read the server-side dialect tree;
            # no settlements => 0.0, classic formula preserved).
            language_fragmentation=self.engine._language_fragmentation(
                law.civilization_id),
        )
        return {
            "estimated_cost": cost,
            "contradictions": contradictions,
            "breakdown_available": True,
            "note": "Стоимость не зависит от времени подачи правки.",

        }

    def _count_preview_conflicts(self, law, canon, action=None) -> int:
        """Conflict count against the current active set (no turn running).

        Mirrors the engine's step-13 rules (§5.1 «конфликт с другими
        законами»): same-kind overlap, contradictory kind pairs, and — for a
        prospective REPEAL — dependency references from surviving laws.
        """
        from .core import LawUserAction as _A, kinds_conflict

        count = 0
        for other in self.store.laws_of(law.civilization_id):
            if other.id == law.id or other.canonical is None:
                continue
            if other.status.value not in ("active", "in_transition"):
                continue
            if canon is not None and (
                    other.canonical.kind == canon.kind
                    or kinds_conflict(canon.kind, other.canonical.kind)):
                count += 1
        if action == _A.REPEAL_LAW:
            for other in self.store.laws_of(law.civilization_id):
                if other.id == law.id or other.status.value not in (
                        "active", "in_transition"):
                    continue
                depends = (
                    law.id in getattr(other.canonical, "refs", ())
                    if other.canonical is not None else False
                ) or (bool(law.title) and law.title in other.raw_text)
                if depends:
                    count += 1
        return count

    # ================= chronology (§15.4) ===================================

    def get_chronology(self, path: str, body: dict) -> dict:
        from .chronology import calculate_turn_span, determine_world_era

        ch = self.chronology
        civs = list(self.civilizations.values())
        next_span = calculate_turn_span(ch, civs)
        return {
            "current_turn": ch.current_turn,
            "current_year": ch.current_year,
            "current_season": ch.current_season.value,
            # Live era (§9): re-derived from the registered civilizations so
            # that setup via POST .../civilizations is reflected immediately,
            # even before the first turn advances chronology.
            "current_era": determine_world_era(ch, civs).value,
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
        # Convenience: if no turn is open and the request supplies the
        # schedule, open it first — one-call turn execution for clients.
        if self.engine.current_turn is None and "started_at" in body \
                and "simulation_cutoff_at" in body:
            self.open_turn(path, body)
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
            "ruler_reports": report.ruler_reports,   # §13 st.22: literary turn report
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
        return {"laws": [self._law_json(l) for l in laws]}

    def get_law_detail(self, path: str, body: dict) -> dict:
        """GET /api/v1/laws/{id} — full client card (§16.2).

        Shows everything §16.2 requires for each law: current version,
        drafts, pending edits, version history, transition periods with the
        REMAINING TIME IN GAME UNITS (years), tradition, norm conflicts and
        folklore effects. All durations are reported in game time, never in
        turn counts (§20 rule 9).
        """
        law_id = _path_id(path, "laws")
        law = self.store.get_law(law_id)
        civ_id = law.civilization_id
        # --- drafts attached to this law (§3.3) ---
        drafts = [
            _draft_json(d) for d in self.store.list_drafts(civ_id)
            if d.target_law_id == law_id
        ]

        # --- pending revisions: which slice will they join? (§15.3, §16.3) ---
        pending = []
        cutoff = (self.engine.current_turn.simulation_cutoff_at
                  if self.engine.current_turn is not None else None)
        for rev in self.store.revisions_for_law(law_id):
            if rev.status != LawRevisionStatus.PENDING:
                continue
            will_apply = ("current_turn" if cutoff is not None
                          and rev.received_at <= cutoff else "next_turn")
            pending.append({**_revision_json(rev), "will_apply_in": will_apply})

        # --- version history (§16.2) ---
        history = [_revision_json(r) for r in self.store.version_history(law_id)]

        # --- transitions & suspensions remaining in GAME time (§11.7, §16.2) ---
        transitions = [
            {"kind": e.kind.value, "remaining_years": e.remaining.years,
             "payload": e.payload}
            for e in self._effects_view()
            if e.payload.get("law_id") == law_id
            and e.kind in (TimedEffectKind.LAW_TRANSITION, TimedEffectKind.SUSPENSION)
        ]

        # --- norm conflicts against the active set (§4.3, §16.2) ---
        from .core import kinds_conflict

        conflicts = []
        for other in self.store.laws_of(civ_id):
            if other.id == law_id or other.canonical is None:
                continue
            if other.status.value not in ("active", "in_transition"):
                continue
            same_kind = (law.canonical is not None
                         and other.canonical.kind == law.canonical.kind)
            matrix_hit = (law.canonical is not None
                          and kinds_conflict(law.canonical.kind, other.canonical.kind))
            if same_kind or matrix_hit:
                conflicts.append({"law_b": other.id, "title": other.title,
                                  "description": "conflicts with an active law"})

        # --- folklore effects touching this law's topic (§11.5, §16.2) ---
        folklore = [
            {"id": f.id, "spread": f.spread, "mutation": f.mutation,
             "conflict_factor": f.conflict_factor}
            for f in self.engine.folklore if f.topic == law_id
        ]

        # --- visual identity tokens bound to this law (TASK-067) ---
        law_tokens = [t.to_json() for t in self.engine.tokens.all()
                      if law_id in set(t.refs.values())]

        return {
            "law": self._law_json(law),
            "drafts": drafts,
            "pending_edits": pending,
            "version_history": history,
            "transitions": transitions,
            "conflicts": conflicts,
            "folklore_effects": folklore,
            "visual_tokens": law_tokens,
        }

    def _law_json(self, l: Law) -> dict:
        return {
            "id": l.id, "title": l.title, "status": l.status.value,
            "tradition": l.tradition, "age_years": l.age_years,
            "raw_text": l.raw_text,
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
        self.engine.timed_effects.append(eff)
        return {"treaty_id": eff.id, "duration_years": eff.remaining.years}

    def declare_war(self, path: str, body: dict) -> dict:
        """§13 step 17: start a war. Its fatigue clock runs in GAME time."""
        import uuid as _uuid

        war = War(
            id=body.get("id") or str(_uuid.uuid4()),
            world_id=body["world_id"],
            attacker_id=body["attacker_id"],
            defender_id=body["defender_id"],
            started_year=self.chronology.current_year,
            annual_fatigue_rate=float(body.get("annual_fatigue_rate", 0.01)),
        )
        self.engine.wars.append(war)
        return {"war_id": war.id, "started_year": war.started_year}

    # ================= image task queue (§13 step 20) ======================

    def list_image_tasks(self, path: str, body: dict) -> dict:
        """GET /api/v1/worlds/{id}/images — poll illustration progress (§16)."""
        _expire_image_leases(self.engine.image_queue)  # reclaim crashed workers
        status_filter = body.get("status")
        tasks = [t.to_json() for t in self.engine.image_queue]
        if status_filter:
            tasks = [t for t in tasks if t["status"] == status_filter]
        return {"tasks": tasks, "pending": sum(1 for t in tasks if t["status"] == "queued")}

    def image_queue_metrics(self, path: str, body: dict) -> dict:
        """GET /api/v1/worlds/{id}/images/metrics — queue depth & render stats.

        Admin-panel view of the local GPU rendering pipeline (§13 step 20):
        backlog per status, per-worker load, reclaim count (expired leases)
        and average wall-clock wait/render times over finished tasks. All
        numbers are derived from task timestamps recorded by claim/complete;
        nothing here changes game state.
        """
        queue = self.engine.image_queue
        _expire_image_leases(queue)
        now = time.time()
        counts = {"queued": 0, "running": 0, "done": 0, "failed": 0}
        by_worker: dict[str, int] = {}
        waits: list[float] = []      # created_at -> started_at
        renders: list[float] = []    # started_at -> finished_at
        reclaimed = 0
        oldest_queued_age = None
        for t in queue:
            counts[t.status] = counts.get(t.status, 0) + 1
            if t.status == "running" and t.claimed_by:
                by_worker[t.claimed_by] = by_worker.get(t.claimed_by, 0) + 1
            if t.attempts > 1:
                reclaimed += t.attempts - 1
            if t.created_at is not None and t.started_at is not None:
                waits.append(t.started_at - t.created_at)
            if t.started_at is not None and t.finished_at is not None:
                renders.append(t.finished_at - t.started_at)
            if t.status == "queued" and t.created_at is not None:
                age = now - t.created_at
                if oldest_queued_age is None or age > oldest_queued_age:
                    oldest_queued_age = age

        def _avg(xs: list[float]) -> float | None:
            return round(sum(xs) / len(xs), 3) if xs else None

        return {
            "counts": counts,
            "backlog": counts["queued"],
            "active_workers": len(by_worker),
            "by_worker": by_worker,
            "reclaimed_leases": reclaimed,
            "oldest_queued_age_seconds": (
                round(oldest_queued_age, 3) if oldest_queued_age is not None else None
            ),
            "avg_wait_seconds": _avg(waits),
            "avg_render_seconds": _avg(renders),
            "finished": {"done": counts["done"], "failed": counts["failed"]},
            "success_rate": (
                round(counts["done"] / (counts["done"] + counts["failed"]), 4)
                if counts["done"] + counts["failed"] else None
            ),
        }

    def claim_image_task(self, path: str, body: dict) -> dict:
        """POST /api/v1/images/{task_id}/claim — local GPU worker takes one job.

        queued -> running; claiming an already-running/finished task is a
        no-op error so parallel renderers never double-process (§13 step 20).
        Images are rendered exclusively on the server's own video cards by
        trusted local workers — no external generation services (§20.4).
        A lease (claimed_by/lease_expires_at) lets crashed workers be
        detected: expired leases release the task back to the queue.
        """
        task_id = _path_id(path, "images")
        _expire_image_leases(self.engine.image_queue)
        for t in self.engine.image_queue:
            if t.id == task_id:
                if t.status != "queued":
                    raise ValueError(f"task {task_id} is not claimable (status={t.status})")
                t.status = "running"
                t.claimed_by = str(body.get("worker_id", "worker"))
                # Timestamps feed the queue metrics endpoint (§13 step 20):
                # created_at is set at enqueue; started_at marks first claim.
                if t.created_at is None:
                    t.created_at = time.time()
                if t.started_at is None:
                    t.started_at = time.time()
                t.attempts += 1
                t.lease_expires_at = time.time() + float(
                    body.get("lease_seconds", IMAGE_LEASE_SECONDS_DEFAULT)
                )
                return {"task": t.to_json()}
        raise ValueError(f"unknown image task: {task_id}")

    def heartbeat_image_task(self, path: str, body: dict) -> dict:
        """POST /api/v1/images/{task_id}/heartbeat — extend the lease.

        Long renders (high-res diffusion on local VRAM) renew their lease so
        the scheduler does not reclaim a task that is still progressing.
        """
        task_id = _path_id(path, "images")
        _expire_image_leases(self.engine.image_queue)
        for t in self.engine.image_queue:
            if t.id == task_id:
                if t.status != "running":
                    raise ValueError(f"task {task_id} is not running (status={t.status})")
                t.lease_expires_at = time.time() + float(
                    body.get("lease_seconds", IMAGE_LEASE_SECONDS_DEFAULT)
                )
                return {"task": t.to_json()}
        raise ValueError(f"unknown image task: {task_id}")

    def complete_image_task(self, path: str, body: dict) -> dict:
        """POST /api/v1/images/{task_id}/complete — worker reports verdict.

        running -> done | failed, with the produced asset reference stored on
        the task for the published report (§13 steps 20–22).
        ``asset_ref`` must point at storage served by this same server
        (local disk / internal object store), never an external service.
        A local renderer may also upload the finished bytes inline
        (``asset_base64``): they are then written into the server-owned
        ``asset_dir`` and the stored reference is replaced by the sanitised
        relative path plus sha256/size metadata.
        """
        task_id = _path_id(path, "images")
        _expire_image_leases(self.engine.image_queue)
        for t in self.engine.image_queue:
            if t.id == task_id:
                if t.status != "running":
                    raise ValueError(f"task {task_id} must be claimed first (status={t.status})")
                success = bool(body.get("success", True))
                meta = None
                if success and body.get("asset_base64") is not None:
                    try:
                        data = base64.b64decode(str(body["asset_base64"]), validate=True)
                    except (binascii.Error, ValueError):
                        raise ValueError("asset_base64 is not valid base64") from None
                    ref = body.get("asset_ref") or f"{t.world_id}/{t.turn_id}/{t.id}.png"
                    meta = _store_asset_bytes(self.asset_dir, ref, data)
                    t.asset_ref = meta["asset_ref"]
                elif body.get("asset_ref"):
                    # Reference-only completion is allowed when the renderer
                    # writes directly into a shared server-local volume; the
                    # path still has to be well-formed and inside the sandbox.
                    if self.asset_dir is not None:
                        root = Path(self.asset_dir).resolve()
                        probe = (root / str(body["asset_ref"]).lstrip("/")).resolve()
                        if not str(probe).startswith(str(root) + "/"):
                            raise ValueError("asset_ref escapes the asset directory")
                    t.asset_ref = str(body["asset_ref"])
                t.status = "done" if success else "failed"
                t.finished_at = time.time()   # metrics: render duration (§13 st.20)
                if not success:
                    t.asset_ref = None
                if body.get("error"):
                    t.payload["error"] = str(body["error"])
                t.claimed_by = None
                t.lease_expires_at = None
                out = {"task": t.to_json()}
                if meta:
                    out["asset"] = meta
                return out
        raise ValueError(f"unknown image task: {task_id}")

    def get_image_asset(self, path: str, body: dict) -> dict:
        """GET /api/v1/images/{task_id}/asset — serve a finished illustration.

        The client receives the bytes straight from the server's own storage
        (base64 payload + media type); nothing is proxied from third parties.
        Only tasks in status ``done`` with a materialised file are served.
        """
        task_id = _path_id(path, "images")
        for t in self.engine.image_queue:
            if t.id == task_id:
                if t.status != "done" or not t.asset_ref:
                    raise ValueError(f"image {task_id} is not ready (status={t.status})")
                if self.asset_dir is None:
                    raise ValueError("asset storage is not configured for this world server")
                root = Path(self.asset_dir).resolve()
                target = (root / str(t.asset_ref)).resolve()
                if not str(target).startswith(str(root) + "/") or not target.is_file():
                    raise ValueError(f"asset file is missing for image {task_id}")
                data = target.read_bytes()
                return {
                    "asset": {
                        "asset_ref": t.asset_ref,
                        "media_type": mimetypes.guess_type(str(target))[0]
                                    or "application/octet-stream",
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "base64": base64.b64encode(data).decode("ascii"),
                    }
                }
        raise ValueError(f"unknown image task: {task_id}")

    # ================= visual identity tokens (TASK-067) ====================

    def register_visual_token(self, path: str, body: dict) -> dict:
        """POST /api/v1/worlds/{id}/tokens — store a face/appearance in RAM.

        Historical persons, invention looks, paintings, places, costumes:
        everything the local GPU renderers need to draw the *same* subject
        twice. Registration is idempotent by (kind, name): re-registering
        refines the existing entry instead of duplicating it, so lore stays
        stable across devices and reconnects.
        """
        token = self.engine.tokens.register(body, now=time.time())
        return {"token": token.to_json()}

    def list_visual_tokens(self, path: str, body: dict) -> dict:
        """GET /api/v1/worlds/{id}/tokens — registry listing / search.

        Query fields in the JSON body: ``q`` (substring over names,
        descriptors, tags), ``kind``, ``tag``, ``limit``. Deterministic
        insertion order; no game state changes (§20.4).
        """
        hits = self.engine.tokens.search(
            query=str(body.get("q", "")),
            kind=body.get("kind"), tag=body.get("tag"),
            limit=int(body.get("limit", 50)))
        return {"tokens": [t.to_json() for t in hits], "count": len(hits)}

    def get_visual_token(self, path: str, body: dict) -> dict:
        """GET /api/v1/tokens/{id} — one token with its consistency context."""
        token = self.engine.tokens.must_get(_path_id(path, "tokens"))
        related = self.engine.tokens.related_to(token)
        return {"token": token.to_json(),
                "related": [t.to_json() for t in related]}

    def update_visual_token(self, path: str, body: dict) -> dict:
        """PUT /api/v1/tokens/{id} — refine an existing identity in place.

        Field-wise merge (absent keys keep prior values); the id is taken
        from the path, so the descriptor/embedding/reference image of e.g.
        «Юлий Цезарь» can be corrected without spawning a second face.
        ``name``/``kind`` are optional here — they are read off the stored
        token when omitted.
        """
        token_id = _path_id(path, "tokens")
        current = self.engine.tokens.must_get(token_id)
        data = dict(body)
        data["id"] = token_id
        data.setdefault("name", current.name)
        data.setdefault("kind", current.kind)
        token = self.engine.tokens.register(data, now=time.time())
        return {"token": token.to_json()}

    def delete_visual_token(self, path: str, body: dict) -> dict:
        """DELETE /api/v1/tokens/{id} — forget one identity."""
        self.engine.tokens.remove(_path_id(path, "tokens"))
        return {"deleted": _path_id(path, "tokens")}

    def token_condition(self, path: str, body: dict) -> dict:
        """GET /api/v1/tokens/{id}/condition — renderer conditioning payload.

        Aggregates the token plus its related identities into the exact
        bundle a local GPU worker needs: prompt fragments, server-local
        reference images, embeddings, negative hints. Read-only (§20.4).
        """
        token = self.engine.tokens.must_get(_path_id(path, "tokens"))
        ids = [token.id] + [t.id for t in self.engine.tokens.related_to(token)]
        cond = self.engine.tokens.condition_for(ids)
        cond["token_id"] = token.id
        return {"condition": cond}

    def task_condition(self, path: str, body: dict) -> dict:
        """GET /api/v1/images/{id}/condition — full conditioning for one task.

        Renderer entry point (§13 step 20): returns the task's stored
        prompt, the token ids selected when it was enqueued and their
        conditioning bundle, so illustrations of recurring subjects are
        byte-consistent without the worker guessing anything.
        """
        task_id = _path_id(path, "images")
        _expire_image_leases(self.engine.image_queue)
        for t in self.engine.image_queue:
            if t.id == task_id:
                ids = list(t.payload.get("token_ids", []))
                cond = self.engine.tokens.condition_for(ids)
                cond["task_id"] = t.id
                cond["prompt"] = t.prompt
                cond["status"] = t.status
                return {"condition": cond}
        raise ValueError(f"unknown image task: {task_id}")

    _ROUTES: dict = {}


def treaty_as_effect(body: dict) -> TimedEffect:
    """§11.6: a treaty is stored as a timed effect whose duration lives in
    GAME time (years), never in a number of turns."""
    import uuid

    return TimedEffect(
        id=body.get("id") or str(uuid.uuid4()),
        world_id=body["world_id"],
        civilization_id=body.get("civilization_id"),
        kind=TimedEffectKind.TREATY,
        remaining=SimDuration(years=float(body["duration_years"])),
        payload={"treaty_kind": body.get("kind", "peace")},
    )


# Backwards-compatible alias for the previous internal spelling.
Treaty_as_effect = treaty_as_effect


def _normalize_path(path: str) -> str:
    """Collapse id segments so routes can be matched generically."""
    parts = [p for p in path.strip("/").split("/") if p]
    out = []
    for i, p in enumerate(parts):
        out.append("{id}" if _looks_like_id(p, parts, i) else p)
    return "/" + "/".join(out)


def _looks_like_id(token: str, parts: list[str], index: int) -> bool:
    known_ids = {"laws", "drafts", "revisions", "worlds", "chronology", "pending",
                 "snapshot", "turns", "report", "api", "v1", "submit", "cancel", "treaties",
                 "wars", "status", "images", "claim", "complete", "heartbeat",
                 "asset", "civilizations", "cost_preview", "metrics", "validate",
                 "tokens", "condition"}
    if token in known_ids:
        return False
    prev = parts[index - 1] if index else ""
    return prev in {"laws", "drafts", "revisions", "worlds", "treaties", "images",
                    "tokens"}


def _parse_action(value: str) -> LawUserAction:
    """Accept both canonical enum names (§3.2 ``RepealLaw``) and snake_case
    aliases (``repeal_law``) so clients never trip over casing."""
    text = str(value)
    try:
        return LawUserAction(text)
    except ValueError:
        pass
    try:
        return LawUserAction[text.upper()]          # REPEAL_LAW form
    except ValueError:
        pass
    # snake/kebab-case -> CamelCase member name ("repeal_law" -> "RepealLaw")
    pascal = "".join(w.capitalize() for w in text.replace("-", "_").split("_"))
    for member in LawUserAction:
        if member.name.lower() == pascal.lower():
            return member
    raise ValueError(f"unknown law action: {value!r}") from None


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
    out = {
        "id": d.id,
        "civilization_id": d.civilization_id,
        "target_law_id": d.target_law_id,
        "raw_text": d.raw_text,
        "status": d.status.value,
        "updated_at": d.updated_at.isoformat(),
    }
    # Echo back the human-friendly title if it was embedded as "[Title] ..."
    if d.raw_text and d.raw_text.startswith("[") and "]" in d.raw_text:
        out["title"] = d.raw_text[1:d.raw_text.index("]")]
    return out


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
    # §3.3/TASK-066: вердикт нейросетевого слоя над текстом черновика.
    ("POST", r"^/api/v1/laws/drafts/\{id\}/validate$", WorldServer.validate_draft),
    ("POST", r"^/api/v1/laws/\{id\}/revisions$", WorldServer.create_revision),
    ("POST", r"^/api/v1/laws/\{id\}/cost_preview$", WorldServer.preview_cost),
    ("GET", r"^/api/v1/laws/\{id\}/revisions$", WorldServer.get_revisions),
    ("POST", r"^/api/v1/laws/\{id\}/revisions/\{id\}/submit$", WorldServer.submit_revision),
    ("POST", r"^/api/v1/laws/\{id\}/revisions/\{id\}/cancel$", WorldServer.cancel_revision),
    ("GET", r"^/api/v1/laws/pending$", WorldServer.get_pending),
    # §16.3: статус правок конкретного закона (в каком ходу будет учтена,
    # перезаписана ли, отклонена; без штрафов за время подачи).
    ("GET", r"^/api/v1/laws/[^/]+/status$", WorldServer.get_law_status),
    # §4.3: снимок активных законов, который читает симуляция (инспектируется
    # клиентом; правки со статусом IncludedInSimulation уже вошли в него).
    ("GET", r"^/api/v1/laws/snapshot$", WorldServer.get_snapshot),
    # Регистрация цивилизаций при настройке мира: без неё чисто HTTP-клиент
    # не может populate мир (§9 — эпоха определяется по цивилизациям).
    ("POST", r"^/api/v1/worlds/\{id\}/civilizations$", WorldServer.create_civilization),
    ("GET", r"^/api/v1/worlds/\{id\}/chronology$", WorldServer.get_chronology),
    ("POST", r"^/api/v1/worlds/\{id\}/turns/open$", WorldServer.open_turn),
    ("POST", r"^/api/v1/worlds/\{id\}/turns/run$", WorldServer.run_turn),
    ("GET", r"^/api/v1/worlds/\{id\}/turns/report$", WorldServer.get_turn_report),
    ("GET", r"^/api/v1/laws$", WorldServer.list_laws),
    ("POST", r"^/api/v1/laws$", WorldServer.create_law),
    # GET /api/v1/laws/{id} — карточка закона (§16.2): версия, черновики,
    # ожидающие правки, история, переходные периоды, традиция, конфликты, фольклор.
    ("GET", r"^/api/v1/laws/[^/]+$", WorldServer.get_law_detail),
    ("POST", r"^/api/v1/treaties$", WorldServer.add_treaty),
    ("POST", r"^/api/v1/wars$", WorldServer.declare_war),
    # §13 step 20: иллюстрации хроники — очередь задач на генерацию.
    ("GET", r"^/api/v1/worlds/\{id\}/images$", WorldServer.list_image_tasks),
    # Метрики очереди рендеринга для админ-панели (глубина, загрузка GPU-
    # воркеров, среднее время ожидания/рендера). Специфичный путь обязан
    # стоять до всеобщего worlds/{id}/{rest}, иначе матчится последний.
    ("GET", r"^/api/v1/worlds/\{id\}/images/metrics$", WorldServer.image_queue_metrics),
    ("POST", r"^/api/v1/images/\{id\}/claim$", WorldServer.claim_image_task),
    ("POST", r"^/api/v1/images/\{id\}/heartbeat$", WorldServer.heartbeat_image_task),
    ("POST", r"^/api/v1/images/\{id\}/complete$", WorldServer.complete_image_task),
    # Готовая иллюстрация раздаётся с собственного хранилища сервера (§20.4).
    ("GET", r"^/api/v1/images/\{id\}/asset$", WorldServer.get_image_asset),
    # TASK-067: память образов мира — лица исторических персонажей, облики
    # изобретений, картины, локации. Реестр живёт в RAM сервера; условия
    # генерации (prompt-фрагменты, референсы, эмбеддинги) читают воркеры.
    ("POST", r"^/api/v1/worlds/\{id\}/tokens$", WorldServer.register_visual_token),
    ("GET", r"^/api/v1/worlds/\{id\}/tokens$", WorldServer.list_visual_tokens),
    ("GET", r"^/api/v1/tokens/\{id\}/condition$", WorldServer.token_condition),
    ("GET", r"^/api/v1/tokens/\{id\}$", WorldServer.get_visual_token),
    ("PUT", r"^/api/v1/tokens/\{id\}$", WorldServer.update_visual_token),
    ("DELETE", r"^/api/v1/tokens/\{id\}$", WorldServer.delete_visual_token),
    # Кондиционирование конкретной задачи очереди — что подмешать в рендер.
    ("GET", r"^/api/v1/images/\{id\}/condition$", WorldServer.task_condition),
]

WorldServer._ROUTES = {
    (m, re.compile(p)): h for m, p, h in _ROUTE_TABLE
}

# GPU render-worker endpoints: these accept the dedicated (weaker) worker
# token instead of full admin rights — a compromised renderer must not be
# able to rewrite laws or run turns.
_WORKER_ROUTES = frozenset({
    WorldServer.claim_image_task,
    WorldServer.heartbeat_image_task,
    WorldServer.complete_image_task,
})


def _match_route(method: str, norm_path: str):
    for (m, pattern), handler in WorldServer._ROUTES.items():
        if m == method and pattern.match(norm_path):
            return handler
    return None
