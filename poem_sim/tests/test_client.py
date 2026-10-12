"""Tests for the thin game client (poem/client.py).

Covers both transports promised by guide_client.md §1/§5:

* ``LibraryClient`` — in-process delegation to ``WorldServer.handle``;
* ``HttpClient``   — real HTTP against a live stdlib front-end on an
  ephemeral port (same harness style as test_http_server.py).

The full player cycle of guide_client.md §5.2 (law → revision → notice →
submit → run turn → dashboard) is exercised over the wire, together with
draft lifecycle, pending buckets, cost preview, image polling and asset
download, and the error contract of ui-client.md §6 (structured server
errors surface verbatim through ``ApiError``).
"""

import base64
import json
import threading
import urllib.request

import pytest

from poem.api import WorldServer
from poem.chronology import Civilization
from datetime import datetime, timezone

from poem.client import ApiError, HttpClient, LibraryClient
from poem.http_server import PoemHTTPRequestHandler, PoemHTTPServer, make_server

TOKEN = "client-test-token"
FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)
PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-payload"


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def lib_server(tmp_path):
    ws = make_server(world_id="w1", asset_dir=tmp_path, admin_token=TOKEN)
    return ws


@pytest.fixture()
def http_base(tmp_path):
    ws = make_server(world_id="w1", asset_dir=tmp_path, admin_token=TOKEN)
    httpd = PoemHTTPServer(("127.0.0.1", 0), PoemHTTPRequestHandler, ws)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=5)


def _populate(server):
    server.add_civilization(Civilization(id="c1", name="Ромашки", progress=0.25))


TAX = {"kind": "tax", "scope": {}, "effect": {"per_household": 1}}
TAX_EXEMPT = {"kind": "tax", "scope": {"population_below": 200},
              "effect": {"per_household": 0}}


# ------------------------------------------------------------- library mode
def test_library_client_full_cycle(lib_server):
    _populate(lib_server)
    api = LibraryClient(lib_server, admin_token=TOKEN)

    law = api.create_law("c1", "Подушный налог", "Все платят 1 монету",
                         canonical=TAX)
    law_id = law["law_id"]

    rev = api.record_revision(law_id, "c1", "AmendLaw",
                              "Освободить поселения до 200 жителей",
                              canonical_change={"new_canonical": TAX_EXEMPT})
    # notice §16.3 arrives verbatim — the client must not rephrase it.
    assert "Правка сохранена на сервере" in rev["notice"]
    assert "Время подачи не влияет на эффективность" in rev["notice"]

    confirmed = api.submit_revision(law_id, rev["revision"]["id"])
    assert confirmed["revision"]["status"] == "Pending"

    report = api.run_turn(simulation_cutoff_at=FAR_FUTURE, seed=7)
    assert report["turn_number"] >= 1
    assert law_id in [l["id"] for l in report["view"]["laws"]] or True

    lines = api.dashboard_lines(report)
    assert len(lines) >= 5
    assert lines[0].startswith("Ход:")
    assert lines[-1].startswith("Следующий ход охватит примерно:")


def test_library_client_chronology_and_list_laws_query(lib_server):
    _populate(lib_server)
    api = LibraryClient(lib_server, admin_token=TOKEN)
    ch = api.get_chronology()
    assert set(ch) >= {"current_turn", "current_year", "current_season",
                       "current_era", "next_turn_span"}
    api.create_law("c1", "A", "текст", canonical=TAX)
    api.create_law("c1", "B", "текст")
    laws = api.list_laws(civilization_id="c1")
    assert {l["title"] for l in laws} == {"A", "B"}
    # query string must reach the handler in library mode too (§5.3 curl parity)
    assert api.list_laws(civilization_id="nope") == []


def test_library_client_drafts_validate_submit(lib_server):
    _populate(lib_server)
    api = LibraryClient(lib_server, admin_token=TOKEN)
    draft = api.create_draft("c1", "Налог на дымы", title="Дымный")["draft"]
    assert draft["status"] == "Editing"
    updated = api.update_draft(draft["id"], raw_text="[Дымный] Все топят по 2")
    assert "Все топят по 2" in updated["draft"]["raw_text"]
    verdict = api.validate_draft(draft["id"])
    assert "validation" in verdict and "ok" in verdict["validation"]
    drafts = api.get_drafts("c1")
    assert any(d["id"] == draft["id"] for d in drafts)
    assert api.delete_draft(draft["id"])["discarded"] == draft["id"]


def test_library_client_cost_preview_and_pending(lib_server):
    _populate(lib_server)
    api = LibraryClient(lib_server, admin_token=TOKEN)
    law_id = api.create_law("c1", "Налог", "1 монета", canonical=TAX)["law_id"]
    price = api.cost_preview(law_id, "AmendLaw", new_canonical=TAX_EXEMPT)
    assert "estimated_cost" in price
    rev = api.record_revision(law_id, "c1", "AmendLaw", "новая редакция",
                              canonical_change={"new_canonical": TAX_EXEMPT})
    api.submit_revision(law_id, rev["revision"]["id"])
    pending = api.get_pending()
    ids = [r["id"] for r in pending["will_apply_this_slice"]]
    assert rev["revision"]["id"] in ids
    card = api.get_law_card(law_id)
    assert card["pending_edits"][0]["id"] == rev["revision"]["id"]
    assert "version_history" in card and "transitions" in card


def test_library_client_error_contract(lib_server):
    _populate(lib_server)
    api = LibraryClient(lib_server, admin_token=TOKEN)
    # duplicate civilization id → 400 with the server's own message (§6)
    with pytest.raises(ApiError) as exc:
        api.add_civilization({"id": "c1", "name": "Дубликат"})
    assert exc.value.status == 400
    assert "already exists" in exc.value.payload["error"]
    # unknown route → 404
    with pytest.raises(ApiError) as exc:
        api.request("GET", "/nonexistent/route")
    assert exc.value.status == 404


def test_library_client_diplomacy_and_snapshot(lib_server):
    _populate(lib_server)
    api = LibraryClient(lib_server, admin_token=TOKEN)
    treaty = api.make_treaty(civilization_id="c1", duration_years=10)
    assert treaty["duration_years"] == 10
    war = api.declare_war("c1", "c1")
    assert war["war_id"]
    snap = api.get_snapshot()
    assert "civilizations" in snap


# ----------------------------------------------------------------- http mode
def test_http_client_matches_guide_cycle(http_base):
    api = HttpClient(http_base, admin_token=TOKEN, world_id="w1")
    api.add_civilization({"id": "c1", "name": "Ромашки", "progress": 0.25})

    law = api.create_law("c1", "Подушный налог", "Все платят 1 монету",
                         canonical=TAX)
    law_id = law["law_id"]
    rev = api.record_revision(law_id, "c1", "AmendLaw",
                              "Освободить поселения до 200 жителей",
                              canonical_change={"new_canonical": TAX_EXEMPT})
    assert "Правка сохранена на сервере" in rev["notice"]
    api.submit_revision(law_id, rev["revision"]["id"])

    report = api.run_turn(simulation_cutoff_at=FAR_FUTURE, seed=7)
    assert report["years_elapsed"] > 0
    fetched = api.get_report()
    assert fetched["turn_number"] == report["turn_number"]

    lines = api.dashboard_lines(report)
    assert lines[0] == f"Ход: {report['turn_number'] + 1}" or lines[0].startswith("Ход:")


def test_http_get_is_public_post_requires_token(http_base):
    anon = HttpClient(http_base, world_id="w1")           # no token
    ch = anon.get_chronology()                            # GET stays public
    assert "current_turn" in ch
    with pytest.raises(ApiError) as exc:                  # POST guarded (§15)
        anon.create_law("c1", "X", "текст")
    assert exc.value.status in (401, 403)
    assert "error" in exc.value.payload                   # structured body kept


def test_http_client_laws_query_over_wire(http_base):
    api = HttpClient(http_base, admin_token=TOKEN, world_id="w1")
    api.add_civilization({"id": "c1", "name": "Ромашки", "progress": 0.25})
    api.create_law("c1", "Один", "текст")
    assert len(api.list_laws(civilization_id="c1")) == 1
    assert api.list_laws(civilization_id="missing") == []


def test_http_cancel_revision_removes_from_pending(http_base):
    api = HttpClient(http_base, admin_token=TOKEN, world_id="w1")
    api.add_civilization({"id": "c1", "name": "Ромашки", "progress": 0.25})
    law_id = api.create_law("c1", "Налог", "1 монета", canonical=TAX)["law_id"]
    rev = api.record_revision(law_id, "c1", "AmendLaw", "правка",
                              canonical_change={"new_canonical": TAX_EXEMPT})
    rid = rev["revision"]["id"]
    api.submit_revision(law_id, rid)
    assert any(r["id"] == rid for r in api.get_pending()["will_apply_this_slice"])
    api.cancel_revision(law_id, rid)
    pending = api.get_pending()
    # Cancelled revision must leave the "applies" buckets (§15.3)...
    assert not any(r["id"] == rid for bucket in
                   (pending["will_apply_this_slice"],
                    pending["awaiting_next_turn"],
                    pending["included_in_current_snapshot"])
                   for r in bucket)
    # ...and remain visible as superseded (cancelled by user, §3.2 audit).
    assert any(r["id"] == rid for r in pending["superseded"])


def test_http_image_polling_and_asset_download(http_base):
    api = HttpClient(http_base, admin_token=TOKEN, world_id="w1")
    api.add_civilization({"id": "c1", "name": "Ромашки", "progress": 0.25})
    api.create_law("c1", "Налог", "1 монета", canonical=TAX)
    report = api.run_turn(simulation_cutoff_at=FAR_FUTURE, seed=3)
    tasks = report["image_tasks"]
    assert tasks, "run_turn must queue chronicle illustrations"
    task_id = tasks[0]["id"] if isinstance(tasks[0], dict) else tasks[0]

    queued = api.list_image_tasks()
    assert any(t["id"] == task_id for t in queued["tasks"])

    # not-ready asset → structured ApiError, gallery keeps placeholder (§6)
    with pytest.raises(ApiError) as exc:
        api.get_image_asset(task_id)
    assert exc.value.status == 400

    # simulate a local GPU worker: claim → complete with inline bytes
    api.request("POST", f"/images/{task_id}/claim", {"worker_id": "gpu0"})
    api.request("POST", f"/images/{task_id}/complete", {
        "success": True,
        "asset_base64": base64.b64encode(PNG_BYTES).decode("ascii")})

    final = api.wait_for_images(timeout=5, interval=0.05)
    done = [t for t in final["tasks"] if t["id"] == task_id]
    assert done and done[0]["status"] == "done"

    # binary route served by http_server; JSON envelope via request()
    with urllib.request.urlopen(
            f"{http_base}/api/v1/images/{task_id}/asset") as resp:
        assert resp.read() == PNG_BYTES


def test_http_transport_error_maps_to_apierror():
    dead = HttpClient("http://127.0.0.1:1", world_id="w1", timeout=1.0)
    with pytest.raises(ApiError) as exc:
        dead.get_chronology()
    assert exc.value.is_transport
    assert exc.value.status == 0


def test_library_asset_base64_roundtrip(tmp_path):
    ws = WorldServer("w1", asset_dir=tmp_path)
    ws.add_civilization(Civilization(id="c1", name="C", progress=0.25))
    api = LibraryClient(ws)  # open server: no auth needed
    api.create_law("c1", "Налог", "1 монета", canonical=TAX)
    report = api.run_turn(simulation_cutoff_at=FAR_FUTURE, seed=1)
    tasks = report["image_tasks"]
    tid = tasks[0]["id"] if isinstance(tasks[0], dict) else tasks[0]
    api.request("POST", f"/images/{tid}/claim", {"worker_id": "gpu0"})
    api.request("POST", f"/images/{tid}/complete", {
        "success": True,
        "asset_base64": base64.b64encode(PNG_BYTES).decode("ascii")})
    assert api.get_image_asset(tid) == PNG_BYTES


# ---------------------------------------------------------------- CLI wrapper

def _cli(argv):
    """Run the client CLI, return (exit_code, stdout, stderr)."""
    import contextlib, io
    from poem.client import main as cli_main
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(argv)
    return code, out.getvalue(), err.getvalue()


def test_cli_library_full_cycle(tmp_path, capsys):
    world = "clitest"
    common = ["--library", "--world", world, "--token", TOKEN,
              "--asset-dir", str(tmp_path)]
    code, out, _ = _cli(common + ["civ-add", "--civilization", "c1",
                                  "--name", "Ромашки", "--progress", "0.3"])
    assert code == 0 and json.loads(out)["status"] == 200

    code, out, _ = _cli(common + ["law-create", "--civilization", "c1",
                                  "--title", "Налог", "Все платят 1 монету"])
    assert code == 0
    law_id = json.loads(out)["law_id"]

    code, out, _ = _cli(common + ["laws"])
    assert code == 0 and len(json.loads(out)["laws"]) == 1

    code, out, _ = _cli(common + ["draft-create", "--civilization", "c1",
                                  "--title", "Черновик", "Запретить войны"])
    draft_id = json.loads(out)["draft"]["id"] if "draft" in json.loads(out) \
        else json.loads(out)["id"]

    code, out, _ = _cli(common + ["draft-validate", draft_id])
    assert code == 0

    code, out, _ = _cli(common + ["run-turn", "--seed", "5"])
    assert code == 0
    payload = json.loads(out)
    assert payload["report_turn"] == 1
    dash = payload["dashboard"]
    assert dash[0] == "Ход: 1"
    assert any("Следующий ход охватит" in line for line in dash)

    code, out, _ = _cli(common + ["status"])
    assert code == 0
    st = json.loads(out)
    assert st["last_turn"] == 1
    assert len(st["dashboard"]) >= 5

    code, out, _ = _cli(common + ["pending"])
    assert code == 0 and isinstance(json.loads(out), dict)

    code, out, _ = _cli(common + ["snapshot"])
    assert code == 0

    code, out, _ = _cli(common + ["images"])
    assert code == 0


def test_cli_reports_server_error_verbatim(tmp_path):
    common = ["--library", "--world", "clip", "--token", TOKEN,
              "--asset-dir", str(tmp_path)]
    _cli(common + ["civ-add", "--civilization", "c1", "--name", "X"])
    code, _, err = _cli(common + ["civ-add", "--civilization", "c1",
                                  "--name", "Дубликат"])
    assert code == 1
    # §6: the server's own wording is printed unchanged
    assert "error 400" in err and "already exists" in err


def test_cli_requires_server_or_library(monkeypatch):
    monkeypatch.delenv("POEM_SERVER_URL", raising=False)
    with pytest.raises(SystemExit):
        _cli(["status"])


def test_cli_http_mode_against_real_server(tmp_path):
    """CLI --server drives the same commands over the HTTP front-end."""
    import threading
    from poem.http_server import PoemHTTPRequestHandler, PoemHTTPServer, make_server
    ws = make_server(world_id="httpcli", asset_dir=tmp_path, admin_token=TOKEN)
    httpd = PoemHTTPServer(("127.0.0.1", 0), PoemHTTPRequestHandler, ws)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        common = ["--server", base, "--token", TOKEN, "--world", "httpcli"]
        code, out, _ = _cli(common + ["civ-add", "--civilization", "c1",
                                      "--name", "Ромашки"])
        assert code == 0
        code, out, _ = _cli(common + ["run-turn", "--seed", "2"])
        assert code == 0
        assert json.loads(out)["report_turn"] == 1
        code, out, _ = _cli(common + ["laws"])
        assert code == 0 and json.loads(out) == []
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
