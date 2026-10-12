"""Thin game client for the Poem server (guide_client.md §3.2/§5, ui-client.md §4).

The client is **deliberately thin** (§20 rule: the server owns every legally
significant draft, revision and version — nothing is stored here).  It offers
two interchangeable transports behind one API surface:

* ``HttpClient`` — talks to a remote ``poem.http_server`` over HTTP using only
  the standard library (``urllib``), so no third-party dependency is added;
* ``LibraryClient`` — embeds the world in-process by delegating straight to
  ``WorldServer.handle(method, path, body)`` (guide_client.md §1, «интеграция
  через библиотеку»).

Both expose the same typed methods mapped onto the «действие → эндпоинт»
table of ui-client.md §4, plus two conveniences:

* ``dashboard_lines()`` renders the five mandatory dashboard strings of
  design.md §16.1 from live chronology data;
* ``wait_for_images()`` polls ``GET /worlds/{id}/images`` until every task
  reaches ``done``/``failed`` (guide_client.md §5.1 step 7) and downloads
  finished assets via ``GET /images/{task_id}/asset``.

Errors: any non-2xx response raises ``ApiError`` carrying the structured
server payload ``{"error": ..., "status": ...}`` so UI layers can show the
exact server text (§6: notices are never rephrased on the client).

Example (remote mode):

    from poem.client import HttpClient

    api = HttpClient("http://localhost:8080", admin_token="...", world_id="w1")
    api.add_civilization({"id": "c1", "name": "Ромашки", "progress": 0.25})
    law = api.create_law("c1", "Подушный налог", "Все платят 1 монету",
                         canonical={"kind": "tax", "scope": {},
                                    "effect": {"per_household": 1}})
    rev = api.record_revision(law["law_id"], "c1", "AmendLaw",
                              "Освободить поселения до 200 жителей",
                              canonical_change={"new_canonical": {
                                  "kind": "tax",
                                  "scope": {"population_below": 200},
                                  "effect": {"per_household": 0}}})
    print(rev["notice"])                    # дословное уведомление §16.3
    api.submit_revision(law["law_id"], rev["revision"]["id"])
    report = api.run_turn(seed=7)           # open+run одним вызовом
    print("\n".join(api.dashboard_lines(report)))
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

API_PREFIX = "/api/v1"


class ApiError(Exception):
    """Non-2xx answer from the server (or a transport failure).

    ``status`` mirrors the HTTP code; ``payload`` keeps the structured error
    body ``{"error": ..., "status": ...}`` verbatim so the UI can display the
    server's own wording (ui-client.md §6).
    """

    def __init__(self, status: int, payload: dict | None = None,
                 transport_error: Exception | None = None) -> None:
        self.status = status
        self.payload = payload or {}
        self.transport_error = transport_error
        message = self.payload.get("error") or f"HTTP {status}"
        super().__init__(message)

    @property
    def is_transport(self) -> bool:
        """True when the request never reached the server (network down)."""
        return self.transport_error is not None


class _BaseClient(ABC):
    """Shared endpoint mapping; subclasses implement the transport only."""

    def __init__(self, world_id: str = "world") -> None:
        self.world_id = world_id

    # ------------------------------------------------------------ transport
    @abstractmethod
    def request(self, method: str, path: str, body: dict | None = None,
                params: dict | None = None) -> dict:
        """Perform one API call; must raise ``ApiError`` on non-2xx."""

    def _world(self, rest: str) -> str:
        return f"/worlds/{urllib.parse.quote(self.world_id)}{rest}"

    # -------------------------------------------------- chronology / reports
    def get_chronology(self) -> dict:
        """GET /worlds/{id}/chronology — turn banner data (§16.1)."""
        return self.request("GET", self._world("/chronology"))

    def get_report(self) -> dict | None:
        """GET /worlds/{id}/turns/report — last published report (§16)."""
        out = self.request("GET", self._world("/turns/report"))
        return out.get("report")

    def dashboard_lines(self, report: dict | None = None) -> list[str]:
        """Five mandatory dashboard strings of §16.1, live-polled.

        Reads ``current_turn / current_era / current_year / next_turn_span``
        from the chronology endpoint; elapsed time comes from the latest
        published report when one exists.
        """
        ch = self.get_chronology()
        if report is None:
            report = self.get_report()
        span = ch["next_turn_span"]
        lines = [
            f"Ход: {ch['current_turn']}",
            f"Эпоха: {ch['current_era'].capitalize()}",
            (f"Прошло времени с прошлого хода: {report['years_elapsed']} лет"
             if report else "Прошло времени с прошлого хода: —"),
            f"Текущий год: {ch['current_year']}",
            f"Следующий ход охватит примерно: {span['years']} лет",
        ]
        if report and report.get("view", {}).get("dashboard", {}).get(
                "next_turn_span_years") not in (None, span["years"]):
            prev = report["view"]["dashboard"]["next_turn_span_years"]
            lines.append(
                f"⚠ благодаря развитию цивилизаций следующий ход охватит "
                f"{span['years']} лет вместо {prev}")
        return lines

    # ------------------------------------------------------- civilizations
    def add_civilization(self, civ: dict) -> dict:
        """POST /worlds/{id}/civilizations (admin; duplicate id → ApiError 400)."""
        return self.request("POST", self._world("/civilizations"), dict(civ))

    # ----------------------------------------------------------------- laws
    def list_laws(self, civilization_id: str | None = None) -> list[dict]:
        """GET /laws?civilization_id= — карточки законов."""
        params = {"civilization_id": civilization_id} if civilization_id else None
        return self.request("GET", "/laws", params=params)["laws"]

    def create_law(self, civilization_id: str, title: str, raw_text: str,
                   canonical: dict | None = None, **extra) -> dict:
        """POST /laws — создать закон (возвращает ``law_id``)."""
        body = {"civilization_id": civilization_id, "title": title,
                "raw_text": raw_text, "canonical": canonical, **extra}
        return self.request("POST", "/laws", body)

    def get_law_card(self, law_id: str) -> dict:
        """GET /laws/{id} — всё для карточки закона §16.2 одной операцией."""
        return self.request("GET", f"/laws/{urllib.parse.quote(law_id)}")

    def get_law_status(self, law_id: str) -> dict:
        """GET /laws/{id}/status — корзины правок закона (§16.3)."""
        return self.request("GET", f"/laws/{urllib.parse.quote(law_id)}/status")

    def get_pending(self) -> dict:
        """GET /laws/pending — шесть корзин ожидающих правок (§15.3)."""
        return self.request("GET", "/laws/pending")

    def get_snapshot(self) -> dict:
        """GET /laws/snapshot — инспектор среза симуляции (§4.3)."""
        return self.request("GET", "/laws/snapshot")

    # --------------------------------------------------------------- drafts
    def create_draft(self, civilization_id: str, raw_text: str,
                     title: str | None = None,
                     target_law_id: str | None = None) -> dict:
        """POST /laws/drafts — черновик ляжет на сервер (§20: только там истина)."""
        body: dict = {"civilization_id": civilization_id, "raw_text": raw_text}
        if title:
            body["title"] = title
        if target_law_id:
            body["target_law_id"] = target_law_id
        return self.request("POST", "/laws/drafts", body)

    def update_draft(self, draft_id: str, raw_text: str | None = None,
                     canonical_draft: dict | None = None) -> dict:
        """PUT /laws/drafts/{id} — автосохранение; ответ сервера — истина."""
        body: dict = {}
        if raw_text is not None:
            body["raw_text"] = raw_text
        if canonical_draft is not None:
            body["canonical_draft"] = canonical_draft
        return self.request("PUT", f"/laws/drafts/{urllib.parse.quote(draft_id)}", body)

    def get_drafts(self, civilization_id: str) -> list[dict]:
        """GET /laws/drafts?civilization_id= — мои черновики (§6: переспросить
        сервер после обрыва связи)."""
        return self.request("GET", "/laws/drafts",
                            params={"civilization_id": civilization_id})["drafts"]

    def delete_draft(self, draft_id: str) -> dict:
        """DELETE /laws/drafts/{id}."""
        return self.request("DELETE", f"/laws/drafts/{urllib.parse.quote(draft_id)}")

    def validate_draft(self, draft_id: str) -> dict:
        """POST /laws/drafts/{id}/validate — вердикт нейросетевого слоя (§3.3)."""
        return self.request("POST", f"/laws/drafts/{urllib.parse.quote(draft_id)}/validate")

    # ------------------------------------------------------------ revisions
    def record_revision(self, law_id: str, civilization_id: str, action: str,
                        raw_text: str, canonical_change: dict | None = None,
                        received_at: datetime | None = None) -> dict:
        """POST /laws/{id}/revisions — записать правку; в ответе ``notice``
        выводится дословно (§16.3, гарантия отсутствия штрафа за время суток)."""
        body: dict = {"civilization_id": civilization_id, "action": action,
                      "raw_text": raw_text}
        if canonical_change is not None:
            body["canonical_change"] = canonical_change
        if received_at is not None:
            body["received_at"] = received_at.isoformat()
        return self.request("POST", f"/laws/{urllib.parse.quote(law_id)}/revisions", body)

    def submit_revision(self, law_id: str, revision_id: str,
                        draft_id: str | None = None) -> dict:
        """POST /laws/{id}/revisions/{rid}/submit — подтвердить правку либо
        превратить валидированный черновик в ревизию (``draft_id``)."""
        body = {"draft_id": draft_id} if draft_id else {}
        return self.request(
            "POST",
            f"/laws/{urllib.parse.quote(law_id)}/revisions/"
            f"{urllib.parse.quote(revision_id)}/submit", body)

    def cancel_revision(self, law_id: str, revision_id: str) -> dict:
        """POST /laws/{id}/revisions/{rid}/cancel — исчезнет из pending."""
        return self.request(
            "POST",
            f"/laws/{urllib.parse.quote(law_id)}/revisions/"
            f"{urllib.parse.quote(revision_id)}/cancel")

    def get_revisions(self, law_id: str) -> list[dict]:
        """GET /laws/{id}/revisions — история ревизий (diff-вью)."""
        return self.request("GET",
                            f"/laws/{urllib.parse.quote(law_id)}/revisions")["revisions"]

    def cost_preview(self, law_id: str, action: str,
                     new_canonical: dict | None = None, transition_years: float = 0.0,
                     urgent: bool = False, raw_text: str = "") -> dict:
        """POST /laws/{id}/cost_preview — цена правки административным
        ресурсом; показывает ровно ту цифру, что возьмёт срез (§20.5)."""
        body = {"action": action, "new_canonical": new_canonical,
                "transition_years": transition_years, "urgent": urgent,
                "raw_text": raw_text}
        return self.request("POST",
                            f"/laws/{urllib.parse.quote(law_id)}/cost_preview", body)

    # -------------------------------------------------------------- turns
    def open_turn(self, started_at: datetime | None = None,
                  simulation_cutoff_at: datetime | None = None,
                  seed: int = 0) -> dict:
        """POST /worlds/{id}/turns/open (admin)."""
        now = datetime.now(timezone.utc)
        body = {
            "started_at": (started_at or now).isoformat(),
            "simulation_cutoff_at": (simulation_cutoff_at or now).isoformat(),
            "seed": seed,
        }
        return self.request("POST", self._world("/turns/open"), body)

    def run_turn(self, started_at: datetime | None = None,
                 simulation_cutoff_at: datetime | None = None,
                 seed: int = 0) -> dict:
        """POST /worlds/{id}/turns/run (admin).

        Если открытого хода нет, клиент сначала вызывает turns/open (§5.1),
        затем turns/run — один вызов для convenience-сценариев.
        """
        now = datetime.now(timezone.utc)
        if started_at is None and simulation_cutoff_at is None:
            # No schedule given: run whatever turn is already open.
            return self.request("POST", self._world("/turns/run"),
                                {"seed": seed})
        cutoff = simulation_cutoff_at or now
        opened_at = started_at or cutoff - timedelta(days=1)
        # Convenience one-call cycle (§5.1): open the turn first, then run it.
        try:
            self.open_turn(opened_at, cutoff, seed)
        except ApiError as exc:
            if exc.status != 409:  # ход уже открыт — нормально
                raise
        body: dict = {
            "seed": seed,
            "started_at": opened_at.isoformat(),
            "simulation_cutoff_at": cutoff.isoformat(),
        }
        return self.request("POST", self._world("/turns/run"), body)

    # ---------------------------------------------------------- diplomacy
    def make_treaty(self, world_id: str | None = None, civilization_id: str | None = None,
                    duration_years: float = 10.0, kind: str = "peace") -> dict:
        """POST /treaties — договор живёт в игровых годах (§11.6)."""
        body = {"world_id": world_id or self.world_id,
                "civilization_id": civilization_id,
                "duration_years": duration_years, "kind": kind}
        return self.request("POST", "/treaties", body)

    def declare_war(self, attacker_id: str, defender_id: str,
                    world_id: str | None = None, **extra) -> dict:
        """POST /wars — усталость от войны течёт в игровом времени."""
        body = {"world_id": world_id or self.world_id,
                "attacker_id": attacker_id, "defender_id": defender_id, **extra}
        return self.request("POST", "/wars", body)

    # ------------------------------------------------------------- images
    def list_image_tasks(self, status: str | None = None) -> dict:
        """GET /worlds/{id}/images?status= — поллинг очереди иллюстраций."""
        params = {"status": status} if status else None
        return self.request("GET", self._world("/images"), params=params)

    def get_image_asset(self, task_id: str) -> bytes:
        """GET /images/{id}/asset — байты готовой картинки (или ApiError 400,
        если задача ещё не done — слот галереи остаётся placeholder, §6)."""
        raise NotImplementedError

    def wait_for_images(self, timeout: float = 120.0, interval: float = 0.5) -> dict:
        """Poll the image queue until every task is done/failed (§5.1 step 7).

        Returns the final ``list_image_tasks`` payload.  The report stays
        readable without illustrations, hence a timeout simply returns the
        last snapshot instead of raising.
        """
        deadline = time.monotonic() + timeout
        while True:
            data = self.list_image_tasks()
            unfinished = [t for t in data["tasks"]
                          if t["status"] not in ("done", "failed")]
            if not unfinished or time.monotonic() >= deadline:
                return data
            time.sleep(interval)


class LibraryClient(_BaseClient):
    """In-process client: delegates directly to ``WorldServer.handle``.

    ``admin_token`` is forwarded through the body field ``auth_token`` —
    the spelling ``WorldServer._extract_credential`` accepts for framework
    hosts that only pass method/path/body (guide_client.md §1).  Omit it for
    a read-only player client: mutating routes then answer 401, exactly like
    over HTTP (§15 security addendum).
    """

    def __init__(self, server, world_id: str | None = None,
                 admin_token: str | None = None) -> None:
        super().__init__(world_id or server.chronology.world_id)
        self.server = server
        self.admin_token = admin_token

    def request(self, method: str, path: str, body: dict | None = None,
                params: dict | None = None) -> dict:
        query = _encode_params(params)
        full = f"{API_PREFIX}{path}" + (("?" + query) if query else "")
        payload = _merge(body, params)
        if self.admin_token and method.upper() != "GET":
            payload.setdefault("auth_token", self.admin_token)
        result = self.server.handle(method, full, payload)
        status = int(result.get("status", 200))
        if status >= 400:
            raise ApiError(status, result)
        return result

    def get_image_asset(self, task_id: str) -> bytes:
        """JSON envelope ``{"asset": {"base64": ...}}`` → raw bytes (§20.4)."""
        import base64
        out = self.request("GET", f"/images/{urllib.parse.quote(task_id)}/asset")
        asset = out.get("asset") or {}
        if not isinstance(asset, dict) or "base64" not in asset:
            raise ApiError(400, out if not out.get("asset") else
                           {"error": "asset envelope without base64 payload"})
        return base64.b64decode(asset["base64"])


class HttpClient(_BaseClient):
    """Remote client over the stdlib HTTP front-end (no third-party deps)."""

    def __init__(self, base_url: str, admin_token: str | None = None,
                 world_id: str = "world", timeout: float = 30.0) -> None:
        super().__init__(world_id)
        self.base_url = base_url.rstrip("/")
        self.admin_token = admin_token
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None,
                params: dict | None = None) -> dict:
        url = f"{self.base_url}{API_PREFIX}{path}"
        query = _encode_params(params)
        if query:
            url += "?" + query
        # GET/DELETE must not carry a body: stdlib http.server answers such
        # requests with 501 UnsupportedMethod ('GET' allowed; 'request_body'
        # specified), so only mutating verbs get JSON payloads.
        has_body = body is not None and method.upper() not in ("GET", "HEAD", "DELETE")
        data = json.dumps(_merge(body, params), ensure_ascii=False).encode("utf-8") \
            if has_body else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.admin_token:
            req.add_header("Authorization", f"Bearer {self.admin_token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                status = int(payload.get("status", resp.status))
                if status >= 400:
                    raise ApiError(status, payload)
                return payload
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = None
            raise ApiError(exc.code, payload, transport_error=None) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                OSError) as exc:
            # ui-client.md §6: «нет связи» — редактор живёт локально, все
            # уже отправленные правки остаются на сервере.
            raise ApiError(0, {"error": f"network unavailable: {exc}"},
                           transport_error=exc) from None

    def get_image_asset(self, task_id: str) -> bytes:
        url = f"{self.base_url}{API_PREFIX}/images/{urllib.parse.quote(task_id)}/asset"
        req = urllib.request.Request(url, method="GET")
        if self.admin_token:
            req.add_header("Authorization", f"Bearer {self.admin_token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = None
            raise ApiError(exc.code, payload) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                OSError) as exc:
            raise ApiError(0, {"error": f"network unavailable: {exc}"},
                           transport_error=exc) from None


# ------------------------------------------------------------------ helpers
def _encode_params(params: dict | None) -> str:
    if not params:
        return ""
    return urllib.parse.urlencode({k: v for k, v in params.items()
                                   if v is not None})


def _merge(body: dict | None, params: dict | None) -> dict:
    """Query parameters double as handler input: ``WorldServer.handle`` reads
    bodies only, so a library client must fold them in explicitly."""
    merged = dict(body or {})
    for k, v in (params or {}).items():
        if v is not None:
            merged.setdefault(k, v)
    return merged


# ---------------------------------------------------------------------------
# CLI wrapper — «python -m poem.client» (guide_client.md §1: тот же тонкий
# клиент из командной строки; удобен для smoke-тестов сервера без браузера).
# ---------------------------------------------------------------------------

def _cli_build_client(args) -> _BaseClient:
    if getattr(args, "library", False):
        from .api import WorldServer  # local import: CLI-only convenience
        ws = WorldServer(args.world, asset_dir=args.asset_dir,
                         admin_token=args.token or "")
        return LibraryClient(ws, world_id=args.world, admin_token=args.token)
    base = args.server
    if not base:
        raise SystemExit("error: pass --server URL or --library "
                         "(or set POEM_SERVER_URL)")
    return HttpClient(base, admin_token=args.token, world_id=args.world)


def _cli_out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


FAR_FUTURE_CLI = datetime(2999, 1, 1, tzinfo=timezone.utc)


def _cmd_status(api: _BaseClient, args) -> None:
    ch = api.get_chronology()
    report = api.get_report()
    out = {"chronology": ch, "dashboard": api.dashboard_lines(report)}
    if report is not None:
        out["last_turn"] = report.get("turn_number")
    _cli_out(out)


def _cmd_laws_list(api: _BaseClient, args) -> None:
    _cli_out(api.list_laws(civilization_id=args.civilization))


def _cmd_law_create(api: _BaseClient, args) -> None:
    _cli_out(api.create_law(args.civilization, args.title, args.text))


def _cmd_draft_create(api: _BaseClient, args) -> None:
    _cli_out(api.create_draft(args.civilization, args.text, title=args.title))


def _cmd_drafts_list(api: _BaseClient, args) -> None:
    _cli_out(api.get_drafts(args.civilization))


def _cmd_draft_validate(api: _BaseClient, args) -> None:
    _cli_out(api.validate_draft(args.draft_id))


def _cmd_pending(api: _BaseClient, args) -> None:
    _cli_out(api.get_pending())


def _cmd_snapshot(api: _BaseClient, args) -> None:
    _cli_out(api.get_snapshot())


def _cmd_civ_add(api: _BaseClient, args) -> None:
    civ = {"id": args.civilization, "name": args.name}
    if args.progress is not None:
        civ["progress"] = args.progress
    _cli_out(api.add_civilization(civ))


def _cmd_run_turn(api: _BaseClient, args) -> None:
    cutoff = datetime.fromtimestamp(args.cutoff_at, tz=timezone.utc) \
        if args.cutoff_at is not None else FAR_FUTURE_CLI
    started = datetime.fromtimestamp(args.started_at, tz=timezone.utc) \
        if args.started_at is not None else cutoff - timedelta(days=1)
    report = api.run_turn(started_at=started, simulation_cutoff_at=cutoff,
                          seed=args.seed)
    _cli_out({"report_turn": report.get("turn_number"),
              "dashboard": api.dashboard_lines(report),
              "notices": report.get("notices", [])})


def _cmd_images(api: _BaseClient, args) -> None:
    _cli_out(api.list_image_tasks(status=args.status))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="poem.client",
        description="Thin game client for the Poem server (guide_client.md §5).")
    parser.add_argument("--server", default=os.environ.get("POEM_SERVER_URL"),
                        help="base URL of a running poem.http_server, "
                             "e.g. http://localhost:8080 (env POEM_SERVER_URL)")
    parser.add_argument("--token", default=os.environ.get("POEM_ADMIN_TOKEN"),
                        help="admin bearer token (env POEM_ADMIN_TOKEN)")
    parser.add_argument("--world", default=os.environ.get("POEM_WORLD_ID", "world"),
                        help="world id (env POEM_WORLD_ID)")
    parser.add_argument("--library", action="store_true",
                        help="in-process mode: embed WorldServer instead of "
                             "talking HTTP (guide_client.md §1 «через библиотеку»)")
    parser.add_argument("--asset-dir",
                        help="asset directory for --library mode")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="banner + dashboard lines (§16.1)")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("laws", help="list law cards")
    p.add_argument("--civilization", default=None)
    p.set_defaults(func=_cmd_laws_list)

    p = sub.add_parser("law-create", help="POST /laws")
    p.add_argument("--civilization", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("text", help="raw legal text as typed by the player")
    p.set_defaults(func=_cmd_law_create)

    p = sub.add_parser("draft-create", help="POST /laws/drafts")
    p.add_argument("--civilization", required=True)
    p.add_argument("--title", default=None)
    p.add_argument("text")
    p.set_defaults(func=_cmd_draft_create)

    p = sub.add_parser("drafts", help="GET /laws/drafts?civilization_id=")
    p.add_argument("--civilization", required=True)
    p.set_defaults(func=_cmd_drafts_list)

    p = sub.add_parser("draft-validate", help="POST /laws/drafts/{id}/validate")
    p.add_argument("draft_id")
    p.set_defaults(func=_cmd_draft_validate)

    p = sub.add_parser("pending", help="GET /laws/pending (six baskets, §15.3)")
    p.set_defaults(func=_cmd_pending)

    p = sub.add_parser("snapshot", help="GET /laws/snapshot (inspector, §4.3)")
    p.set_defaults(func=_cmd_snapshot)

    p = sub.add_parser("civ-add", help="POST /worlds/{id}/civilizations")
    p.add_argument("--civilization", required=True, help="new civilization id")
    p.add_argument("--name", required=True)
    p.add_argument("--progress", type=float, default=None,
                   help="0..1 progress used for era determination (§7.2)")
    p.set_defaults(func=_cmd_civ_add)

    p = sub.add_parser("run-turn", help="open+run a turn, print dashboard")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--started-at", type=float, default=None, metavar="UNIX_TS",
                   help="turn start instant (unix seconds); default: cutoff-1d")
    p.add_argument("--cutoff-at", type=float, default=None, metavar="UNIX_TS",
                   help="simulation cutoff instant (unix seconds); default: far "
                        "future so the first turn actually runs")
    p.set_defaults(func=_cmd_run_turn)

    p = sub.add_parser("images", help="GET /worlds/{id}/images (queue)")
    p.add_argument("--status", default=None)
    p.set_defaults(func=_cmd_images)

    args = parser.parse_args(argv)
    api = _cli_build_client(args)
    try:
        args.func(api, args)
    except ApiError as exc:
        # §6: show the server notice verbatim, never rephrase it.
        print(f"error {exc.status}: {exc.payload.get('error') if exc.payload else exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
