"""Production HTTP front-end for ``WorldServer`` (guide_server.md §5/§6).

The domain layer in ``poem/api.py`` is deliberately framework-agnostic:
every handler speaks plain JSON dicts through ``WorldServer.handle``.  This
module supplies the missing production wiring using **only the Python
standard library** — no FastAPI/aiohttp dependency is introduced, keeping the
install story of the core package unchanged (§20: server owns all state; the
GPU stack lives in a separate worker venv).

Served behaviour:

* All request/response bodies are JSON; unknown paths return the same
  structured error shape as the handlers (``{"status": ..., "error": ...}``).
* Authentication is *not* re-implemented here: mutating routes are guarded by
  the admin credential checks inside ``WorldServer.handle`` (§15 security
  addendum), fed with the standard ``Authorization``/``X-Auth-Token`` headers.
* Finished illustrations are served as real binary PNG bodies at
  ``GET /api/v1/images/{id}/asset`` (the JSON envelope stays available to
  framework hosts); everything else goes through ``handle`` verbatim.
* ``GET /healthz`` — liveness probe for reverse proxies / systemd.

Run:

    python -m poem.http_server --host 0.0.0.0 --port 8080 \
        --world-id europa --asset-dir /var/lib/poem/assets

Environment (same names as guide_server.md §4.4/§4.4a):

    POEM_ADMIN_TOKEN            required unless POEM_ALLOW_OPEN_SERVER=1
    POEM_IMAGE_WORKER_TOKEN     optional dedicated GPU-worker credential
    POEM_WORLD_ID               default world id (single-world deployment)
    POEM_ASSET_DIR              directory holding rendered illustrations
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .api import ADMIN_TOKEN_ENV, WorldServer, _path_id
from .core import EraSource, GameSpeed, SeasonId, TurnStatus

# ---------------------------------------------------------------------------
# Static web client (ui-client.md §8): the SPA bundle is served by the same
# server next to /api/v1 — same origin, no CORS, no external CDNs, so the
# game stays playable on an air-gap server.  The directory lives inside the
# package ("poem/web") so `pip install poem-sim` ships the UI as package data.
# ---------------------------------------------------------------------------
WEB_DIR = Path(__file__).resolve().parent / "web"
_INDEX_HTML: bytes | None = None


def _index_html() -> bytes:
    global _INDEX_HTML
    if _INDEX_HTML is None:
        index = WEB_DIR / "index.html"
        _INDEX_HTML = index.read_bytes() if index.is_file() else b""
    return _INDEX_HTML


def make_server(
    world_id: str = "world",
    asset_dir: str | Path | None = None,
    game_speed: str | None = None,
    era_source: str | None = None,
    season: str | None = None,
    admin_token: str | None = None,
    worker_token: str | None = None,
) -> WorldServer:
    """Build a ``WorldServer`` from explicit args / environment defaults."""
    kwargs: dict = {"asset_dir": asset_dir}
    if game_speed:
        kwargs["game_speed"] = GameSpeed(game_speed)
    if era_source:
        kwargs["era_source"] = EraSource(era_source)
    if season:
        kwargs["season"] = SeasonId(season)
    if admin_token is not None:
        kwargs["admin_token"] = admin_token
    if worker_token is not None:
        kwargs["worker_token"] = worker_token
    return WorldServer(world_id, **kwargs)


class PoemHTTPRequestHandler(BaseHTTPRequestHandler):
    """Maps HTTP verbs onto ``WorldServer.handle``. Threaded server; each
    request is dispatched synchronously against the single shared
    ``WorldServer`` instance (CPython's GIL serialises the read-modify-write
    sequences inside each handler call)."""

    server: "PoemHTTPServer"          # type: ignore[assignment]

    protocol_version = "HTTP/1.1"
    MAX_BODY_BYTES = 64 * 1024 * 1024  # generous cap: image uploads ride base64 JSON

    # ------------------------------------------------------------ helpers
    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _binary(self, status: int, body: bytes, media_type: str,
                extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > self.MAX_BODY_BYTES:
            raise ValueError(f"request body too large ({length} bytes)")
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def _headers_dict(self) -> dict:
        return {k: v for k, v in self.headers.items()}

    # --------------------------------------------------------- asset route
    def _try_asset_binary(self) -> bool:
        """Serve ``GET /api/v1/images/{id}/asset`` as raw bytes. Returns
        True when this branch produced the response."""
        path = self.path.split("?", 1)[0]
        if not (path.startswith("/api/v1/images/") and path.endswith("/asset")):
            return False
        ws = self.server.world_server
        try:
            task_id = _path_id(path, "images")
        except ValueError:
            return False
        for t in ws.engine.image_queue:
            if t.id != task_id:
                continue
            if t.status != "done" or not t.asset_ref or ws.asset_dir is None:
                return False          # fall back to JSON handler (structured 400)
            root = Path(ws.asset_dir).resolve()
            target = (root / str(t.asset_ref)).resolve()
            if not str(target).startswith(str(root) + "/") or not target.is_file():
                return False
            data = target.read_bytes()
            media = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self._binary(200, data, media, {"ETag": f'"{t.id}-{len(data)}"'})
            return True
        return False

    # ------------------------------------------------------- turn schedule
    def _turn_schedule(self) -> None:
        """Expose the *technical schedule* of the open turn (§6.1).

        ``simulation_cutoff_at`` is a wall-clock scheduling marker only — it
        orders edits deterministically and grants no time-of-day bonuses or
        penalties (§2).  The dashboard's cutoff timer and the interval-shift
        banner read this endpoint; both are plain GETs so the thin client can
        poll them without credentials.
        """
        ws = self.server.world_server
        turn = ws.engine.current_turn
        last_report = ws.published_reports[-1] if ws.published_reports else None
        payload: dict = {"turn_open": turn is not None}
        if turn is not None:
            payload.update({
                "turn_id": turn.id,
                "turn_number": turn.number,
                "status": turn.status.value,
                "processing": turn.status in (TurnStatus.SIMULATING,
                                              TurnStatus.FINALIZING),
                "started_at": turn.started_at.isoformat(),
                "simulation_cutoff_at": turn.simulation_cutoff_at.isoformat(),
                "planned_span_years": turn.turn_span.years,
                "note": ("Срез хода — техническое расписание (§6.1). "
                         "Время подачи правки не влияет на эффективность."),
            })
        if last_report is not None:
            view = last_report.get("view") or {}
            dash = view.get("dashboard") or {}
            payload["last_report"] = {
                "turn_number": last_report.get("turn_number"),
                "years_elapsed": last_report.get("years_elapsed"),
                "span_years_next": dash.get("next_turn_span_years"),
            }
        self._json(200, payload)

    # ------------------------------------------------------ static client
    def _try_static(self) -> bool:
        """Serve the bundled SPA (ui-client.md §8: same origin as /api/v1)."""
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path.startswith("/api/"):
            return False
        if path in ("/", "/index.html"):
            html = _index_html()
            if not html:
                return False
            self._binary(200, html, "text/html; charset=utf-8",
                         {"Cache-Control": "no-cache"})
            return True
        candidate = (WEB_DIR / path.lstrip("/")).resolve()
        root = WEB_DIR.resolve()
        if str(candidate).startswith(str(root) + os.sep) and candidate.is_file():
            media = (mimetypes.guess_type(str(candidate))[0]
                     or "application/octet-stream")
            if media.startswith("text/") or media == "application/javascript":
                media += "; charset=utf-8"
            self._binary(200, candidate.read_bytes(), media,
                         {"Cache-Control": "no-cache"})
            return True
        # Unknown non-API path: SPA fallback to index.html when the client
        # bundle is installed (deep links survive a page refresh); otherwise
        # fall through to the JSON router (structured 404).
        if path.startswith("/admin") and _index_html():
            self._binary(200, _index_html(), "text/html; charset=utf-8",
                         {"Cache-Control": "no-cache"})
            return True
        return False

    # ------------------------------------------------------------- verbs
    def do_GET(self) -> None:      # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/healthz":
            ws = self.server.world_server
            self._json(200, {"status": "ok",
                             "world": ws.chronology.world_id,
                             "turn": ws.chronology.current_turn})
            return
        if self.path.split("?", 1)[0] == "/api/v1/turn/schedule":
            self._turn_schedule()
            return
        if self._try_asset_binary():
            return
        if self._try_static():
            return
        self._dispatch("GET")

    def do_POST(self):             # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):              # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self):            # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self):           # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            body = self._read_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"status": 400, "error": str(exc)})
            return
        result = self.server.world_server.handle(method, self.path, body,
                                                 headers=self._headers_dict())
        status = int(result.get("status", 200))
        self._json(status, result)

    # Quieter default logging (one line per request on stderr).
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


class PoemHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_class, world_server: WorldServer):
        super().__init__(addr, handler_class)
        self.world_server = world_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="poem.http_server",
        description="Stdlib HTTP front-end for the Poem world server.")
    parser.add_argument("--host", default=os.environ.get("POEM_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("POEM_PORT", "8080")))
    parser.add_argument("--world-id", default=os.environ.get("POEM_WORLD_ID", "world"))
    parser.add_argument("--asset-dir", default=os.environ.get("POEM_ASSET_DIR"))
    parser.add_argument("--speed", default=os.environ.get("POEM_GAME_SPEED"),
                        choices=[s.value for s in GameSpeed],
                        help="game speed (default: standard)")
    parser.add_argument("--era-source", default=os.environ.get("POEM_ERA_SOURCE"),
                        choices=[e.value for e in EraSource],
                        help="era determination source (default: median_active_civilizations)")
    parser.add_argument("--season", default=os.environ.get("POEM_SEASON"),
                        choices=[s.value for s in SeasonId],
                        help="first season of the world's year (alien calendars)")
    parser.add_argument("--insecure-open-server", action="store_true",
                        help="allow serving without POEM_ADMIN_TOKEN (dev only)")
    args = parser.parse_args(argv)

    admin_token = os.environ.get(ADMIN_TOKEN_ENV, "")
    if not admin_token and not args.insecure_open_server \
            and os.environ.get("POEM_ALLOW_OPEN_SERVER", "") != "1":
        parser.error(
            f"{ADMIN_TOKEN_ENV} is not set — refusing to start a mutating "
            "server without an admin credential. Export the token or pass "
            "--insecure-open-server for local development.")

    ws = make_server(
        world_id=args.world_id,
        asset_dir=args.asset_dir,
        game_speed=args.speed,
        era_source=args.era_source,
        season=args.season,
        admin_token=admin_token,
    )
    httpd = PoemHTTPServer((args.host, args.port), PoemHTTPRequestHandler, ws)
    print(f"poem server listening on http://{args.host}:{args.port} "
          f"(world '{args.world_id}', "
          f"auth={'admin token required' if admin_token else 'OPEN (dev)'})",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
