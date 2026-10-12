"""End-to-end tests for the stdlib HTTP front-end (poem/http_server.py).

A real ThreadingHTTPServer is started on an ephemeral port and driven with
urllib — no test-only fakes, so headers/status codes are verified over the
wire exactly as a client would see them.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from poem.api import WorldServer
from poem.http_server import PoemHTTPRequestHandler, PoemHTTPServer, make_server

TOKEN = "s3cret-admin-token"


@pytest.fixture()
def server(tmp_path):
    ws = make_server(world_id="testworld", asset_dir=tmp_path, admin_token=TOKEN)
    httpd = PoemHTTPServer(("127.0.0.1", 0), PoemHTTPRequestHandler, ws)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=5)


def call(base, method, path, body=None, token=None):
    req = urllib.request.Request(base + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, data=data) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, {"raw": payload}


def test_healthz(server):
    status, payload = call(server, "GET", "/healthz")
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["world"] == "testworld"


def test_get_is_public_and_posts_require_admin(server):
    # GET chronology without any credential works (thin client polls state).
    status, payload = call(server, "GET", "/api/v1/worlds/testworld/chronology")
    assert status == 200
    assert payload["current_turn"] == 0
    # Mutating route without token → 401 structured error.
    status, payload = call(server, "POST", "/api/v1/laws/drafts",
                           {"civ_id": "rome", "title": "T", "text": "words"})
    assert status == 401
    assert "error" in payload
    # Wrong token → 403.
    status, payload = call(server, "POST", "/api/v1/laws/drafts",
                           {"civ_id": "rome", "title": "T", "text": "words"},
                           token="wrong")
    assert status == 403
    # Correct bearer token → success.
    status, payload = call(server, "POST", "/api/v1/laws/drafts",
                           {"civ_id": "rome", "title": "T", "text": "words"},
                           token=TOKEN)
    assert status == 200
    assert payload["draft"]["title"] == "T"


def test_civilization_setup_over_http(server):
    status, _ = call(server, "POST", "/api/v1/worlds/testworld/civilizations",
                    {"id": "rome", "name": "Rome", "progress": 120.0},
                    token=TOKEN)
    assert status == 200
    status, payload = call(server, "GET", "/api/v1/worlds/testworld/chronology")
    assert status == 200
    assert payload["current_era"]  # era derived from the registered civ


def test_unknown_route_returns_structured_404(server):
    status, payload = call(server, "GET", "/api/v1/nope")
    assert status == 404
    assert "no route" in payload["error"]


def test_malformed_json_body_is_400(server):
    req = urllib.request.Request(server + "/api/v1/laws/drafts", method="POST",
                                 data=b"{not json",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {TOKEN}"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 400


def test_make_server_env_config(monkeypatch, tmp_path):
    monkeypatch.setenv("POEM_ADMIN_TOKEN", "tok-env")
    ws = make_server(world_id="w2", asset_dir=tmp_path, game_speed="epic",
                     era_source="leading_civilization", season="long_night")
    assert isinstance(ws, WorldServer)
    assert ws.admin_token == "tok-env"
    assert ws.chronology.game_speed.value == "epic"
    assert ws.chronology.era_source.value == "leading_civilization"
    assert ws.chronology.current_season.value == "long_night"


def test_cli_refuses_open_server_without_token(monkeypatch):
    from poem.http_server import main
    monkeypatch.delenv("POEM_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("POEM_ALLOW_OPEN_SERVER", raising=False)
    with pytest.raises(SystemExit):
        main(["--port", "0"])   # argparse.error → SystemExit(2)


def _raw_get(base, path):
    req = urllib.request.Request(base + path)
    with urllib.request.urlopen(req) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def test_static_bundle_serves_tutorial_and_index_links_it(server):
    """Rev 4 §9bis: туториал политической системы отдаётся тем же origin."""
    status, ctype, body = _raw_get(server, "/js/tutorial.js")
    assert status == 200
    assert "javascript" in ctype and "utf-8" in ctype
    assert b"openPoliticsTutorial" in body
    status, ctype, html = _raw_get(server, "/")
    assert status == 200 and ctype.startswith("text/html")
    assert b"/js/tutorial.js" in html
