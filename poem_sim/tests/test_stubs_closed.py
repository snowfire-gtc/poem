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
    assert done["task"]["payload"]["asset_ref"] == "assets/turn1_hero.png"

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
