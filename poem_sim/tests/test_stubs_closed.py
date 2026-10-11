"""Regression tests for §16.3 status feed and the §13-step-20 image queue.

These endpoints close the last stub-shaped gaps found by auditing the code
base: `GET /api/v1/laws/{id}/status` (§16.3) previously returned 404, and
image tasks were generated per turn but never stored server-side or exposed
to renderers/clients.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.api import WorldServer
from poem.chronology import Civilization
from poem.core import (CanonicalLaw, CanonicalLawChange, Law, LawUserAction)


DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


def make_server():
    srv = WorldServer("w1")
    srv.add_civilization(Civilization(id="c1", name="Rivers", progress=0.25))
    srv.handle("POST", "/api/v1/laws", {
        "id": "law-tax", "civilization_id": "c1", "title": "Head tax",
        "raw_text": "Every settlement pays.", "canonical": {"kind": "tax"},
    })
    return srv


def _revision(srv, hour, action=LawUserAction.AMEND_LAW, text="Exempt small towns."):
    return srv.handle("POST", "/api/v1/laws/law-tax/revisions", {
        "civilization_id": "c1", "action": action.value, "raw_text": text,
        "canonical_change": {"action": action.value},
        "received_at": DAY.replace(hour=hour).isoformat(),
    })["revision"]["id"]


# ---------------------------------------------------------------------------
# §16.3 GET /api/v1/laws/{id}/status
# ---------------------------------------------------------------------------

def test_law_status_endpoint_categorises_edits_against_cutoff():
    srv = make_server()
    cutoff = DAY.replace(hour=20)
    early = _revision(srv, 9)      # before cutoff
    late = _revision(srv, 21)      # after cutoff -> next turn, no penalty

    srv.handle("POST", "/api/v1/worlds/w1/turns/open", {
        "started_at": DAY.isoformat(), "simulation_cutoff_at": cutoff.isoformat()})

    res = srv.handle("GET", "/api/v1/laws/law-tax/status")
    assert res["status"] == 200
    body = res
    ids_will = [r["id"] for r in body["will_apply_this_slice"]]
    ids_deferred = [r["id"] for r in body["awaiting_next_turn"]]
    assert ids_will == [early]
    assert ids_deferred == [late]
    assert body["law_status"] == "active"
    assert body["turn_open"] is True
    joined = "\n".join(body["notices"])
    # §16.3 required texts: accepted notice AND deferral notice w/o penalty
    assert "Время подачи не влияет на эффективность" in joined
    assert "Штрафа за время суток нет" in joined


def test_law_status_shows_superseded_and_snapshot_membership():
    srv = make_server()
    a = _revision(srv, 9, text="Version A.")
    b = _revision(srv, 10, text="Version B.")   # later seq wins over A
    cutoff = DAY.replace(hour=20)
    srv.handle("POST", "/api/v1/worlds/w1/turns/open", {
        "started_at": DAY.isoformat(), "simulation_cutoff_at": cutoff.isoformat()})
    srv.engine.prepare_snapshot(now=cutoff)     # snapshot taken, not run yet

    body = srv.handle("GET", "/api/v1/laws/law-tax/status")
    assert [r["id"] for r in body["included_in_current_snapshot"]] == [b]
    assert [r["id"] for r in body["superseded"]] == [a]


# ---------------------------------------------------------------------------
# §13 step 20: image task queue lifecycle
# ---------------------------------------------------------------------------

def test_image_tasks_are_queued_and_progress_via_api():
    srv = make_server()
    rev = _revision(srv, 9)
    report = srv.handle("POST", "/api/v1/worlds/w1/turns/run", {
        "started_at": DAY.isoformat(),
        "simulation_cutoff_at": DAY.replace(hour=20).isoformat()})
    assert report["status"] == 200

    listed = srv.handle("GET", "/api/v1/worlds/w1/images")
    tasks = listed["tasks"]
    # hero + one vignette for the applied revision
    assert len(tasks) == 2
    kinds = {t["payload"]["kind"] for t in tasks}
    assert kinds == {"hero", "law_vignette"}
    assert all(t["status"] == "queued" for t in tasks)
    assert listed["pending"] == 2

    hero = next(t for t in tasks if t["payload"]["kind"] == "hero")
    claimed = srv.handle("POST", f"/api/v1/images/{hero['id']}/claim", {})
    assert claimed["task"]["status"] == "running"
    # double claim must fail — parallel renderers never process twice
    again = srv.handle("POST", f"/api/v1/images/{hero['id']}/claim", {})
    assert again["status"] == 400

    done = srv.handle("POST", f"/api/v1/images/{hero['id']}/complete",
                      {"success": True, "asset_ref": "assets/turn1_hero.png"})
    assert done["task"]["status"] == "done"
    assert done["task"]["asset_ref"] == "assets/turn1_hero.png"

    failed = srv.handle("POST", f"/api/v1/images/{hero['id']}/complete", {})
    assert failed["status"] == 400   # already finished

    filt = srv.handle("GET", "/api/v1/worlds/w1/images", {"status": "queued"})
    assert [t["payload"]["kind"] for t in filt["tasks"]] == ["law_vignette"]
    # vignette belongs to the revision that was actually applied this turn
    assert filt["tasks"][0]["payload"]["revision_id"] == rev


def test_unknown_image_task_is_rejected():
    srv = make_server()
    res = srv.handle("POST", "/api/v1/images/nope/claim", {})
    assert res["status"] == 400


def _run_turn(srv):
    """Open and cut one logical turn through the public HTTP routes (§16)."""
    srv.handle("POST", "/api/v1/worlds/w1/turns/open", {
        "started_at": DAY.isoformat(),
        "simulation_cutoff_at": DAY.replace(hour=23).isoformat(),
    })
    res = srv.handle("POST", "/api/v1/worlds/w1/turns/run", {})
    assert res["status"] == 200, res
    return res


def _queued_hero(srv):
    tasks = srv.handle("GET", "/api/v1/worlds/w1/images")["tasks"]
    return next(t for t in tasks if t["payload"]["kind"] == "hero")


def test_image_claim_records_worker_lease():
    """Local GPU workers hold an exclusive lease on a claimed task."""
    srv = make_server()
    # produce image tasks for turn 1
    _revision(srv, hour=9)
    _run_turn(srv)
    hero = _queued_hero(srv)

    res = srv.handle("POST", f"/api/v1/images/{hero['id']}/claim",
                     {"worker_id": "gpu-renderer-1", "lease_seconds": 30})
    task = res["task"]
    assert task["status"] == "running"
    assert task["claimed_by"] == "gpu-renderer-1"
    assert task["lease_expires_at"] is not None

    hb = srv.handle("POST", f"/api/v1/images/{hero['id']}/heartbeat",
                    {"lease_seconds": 60})
    assert hb["status"] == 200
    assert hb["task"]["lease_expires_at"] >= task["lease_expires_at"]

    bad = srv.handle("POST", f"/api/v1/images/{hero['id']}/heartbeat", {})
    # heartbeat after completion is invalid; complete while running is ok
    fin = srv.handle("POST", f"/api/v1/images/{hero['id']}/complete",
                     {"success": True, "asset_ref": "assets/h.png"})
    assert fin["task"]["claimed_by"] is None
    assert fin["task"]["asset_ref"] == "assets/h.png"
    late = srv.handle("POST", f"/api/v1/images/{hero['id']}/heartbeat", {})
    assert late["status"] == 400


def test_expired_lease_returns_task_to_queue(monkeypatch):
    """A crashed renderer must never strand a task: expired lease -> queued."""
    import poem.api as api

    srv = make_server()
    _revision(srv, hour=9)
    _run_turn(srv)
    hero = _queued_hero(srv)

    srv.handle("POST", f"/api/v1/images/{hero['id']}/claim",
               {"worker_id": "gpu-dead", "lease_seconds": 1})
    assert srv.handle("GET", "/api/v1/worlds/w1/images",
                      {"status": "queued"})["pending"] == 1  # vignette only

    # simulate the lease deadline passing (crashed worker, no heartbeat)
    monkeypatch.setattr(api.time, "time", lambda: 1e12)
    listed = srv.handle("GET", "/api/v1/worlds/w1/images")
    hero_after = next(t for t in listed["tasks"] if t["id"] == hero["id"])
    assert hero_after["status"] == "queued"
    assert hero_after["claimed_by"] is None

    # another live worker can now pick the orphaned job up
    retry = srv.handle("POST", f"/api/v1/images/{hero['id']}/claim",
                       {"worker_id": "gpu-renderer-2"})
    assert retry["task"]["status"] == "running"
    assert retry["task"]["claimed_by"] == "gpu-renderer-2"


# ---------------------------------------------------------------------------
# §20.4 asset storage: local renderers upload bytes; server serves them back
# ---------------------------------------------------------------------------

def _hero_after_turn(tmp_path=None):
    srv = WorldServer("w1", asset_dir=tmp_path) if tmp_path is not None else make_server()
    if tmp_path is not None:
        srv.add_civilization(Civilization(id="c1", name="Rivers", progress=0.25))
        srv.handle("POST", "/api/v1/laws", {
            "id": "law-tax", "civilization_id": "c1", "title": "Head tax",
            "raw_text": "Every settlement pays.", "canonical": {"kind": "tax"},
        })
        _revision(srv, hour=9)
        _run_turn(srv)
    return srv, _queued_hero(srv)


def test_complete_uploads_asset_bytes_and_get_serves_them(tmp_path):
    """Inline base64 upload lands in the server-owned dir and is served back."""
    import base64 as b64
    srv, hero = _hero_after_turn(tmp_path)
    png = b"\x89PNG\r\n\x1a\n" + b"fake-encoder-output"

    srv.handle("POST", f"/api/v1/images/{hero['id']}/claim",
               {"worker_id": "gpu-renderer-1"})
    fin = srv.handle("POST", f"/api/v1/images/{hero['id']}/complete",
                     {"success": True, "asset_ref": "w1/t1/hero.png",
                      "asset_base64": b64.b64encode(png).decode()})
    assert fin["status"] == 200
    assert fin["task"]["asset_ref"] == "w1/t1/hero.png"
    assert fin["asset"]["bytes"] == len(png)
    assert (tmp_path / "w1/t1/hero.png").read_bytes() == png

    got = srv.handle("GET", f"/api/v1/images/{hero['id']}/asset")
    assert got["status"] == 200
    assert b64.b64decode(got["asset"]["base64"]) == png
    assert got["asset"]["media_type"] == "image/png"


def test_asset_ref_traversal_is_rejected(tmp_path):
    """A renderer cannot write outside the server's asset sandbox (§20.4)."""
    import base64 as b64
    srv, hero = _hero_after_turn(tmp_path)
    payload = b64.b64encode(b"x" * 8).decode()

    srv.handle("POST", f"/api/v1/images/{hero['id']}/claim", {"worker_id": "w"})
    bad = srv.handle("POST", f"/api/v1/images/{hero['id']}/complete",
                     {"success": True, "asset_ref": "../escape.png",
                      "asset_base64": payload})
    assert bad["status"] == 400
    assert not (tmp_path.parent / "escape.png").exists()
    # task stays running after a rejected completion — nothing was written
    listed = srv.handle("GET", "/api/v1/worlds/w1/images")
    t = next(x for x in listed["tasks"] if x["id"] == hero["id"])
    assert t["status"] == "running"


def test_asset_not_ready_until_done(tmp_path):
    """GET asset on a queued task is a 400; failed tasks keep no asset."""
    import base64 as b64
    srv, hero = _hero_after_turn(tmp_path)
    early = srv.handle("GET", f"/api/v1/images/{hero['id']}/asset")
    assert early["status"] == 400

    srv.handle("POST", f"/api/v1/images/{hero['id']}/claim", {"worker_id": "w"})
    fail = srv.handle("POST", f"/api/v1/images/{hero['id']}/complete",
                      {"success": False, "error": "CUDA out of memory"})
    assert fail["task"]["status"] == "failed"
    assert fail["task"]["asset_ref"] is None
    assert fail["task"]["payload"]["error"] == "CUDA out of memory"
    after = srv.handle("GET", f"/api/v1/images/{hero['id']}/asset")
    assert after["status"] == 400
