"""Tests for TASK-061 (server-side drafts/revisions) and §15 API behaviour."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.api import WorldServer
from poem.chronology import Civilization
from poem.core import CanonicalLaw, DraftStatus, Law, LawUserAction
from poem.laws import LawStore, LawStoreError

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# TASK-061 — серверные черновики и ревизии
# ---------------------------------------------------------------------------

def test_drafts_are_server_side_and_cross_device():
    store = LawStore()
    store.add_law(Law(id="l1", civilization_id="c1", title="T", raw_text="x", canonical=None))
    draft = store.create_draft("c1", "Exempt small settlements", target_law_id="l1")
    # Another "device" (a second client instance) sees the same draft by id:
    from_device_b = store.get_draft(draft.id)
    assert from_device_b.raw_text == "Exempt small settlements"
    store.update_draft(draft.id, raw_text="Exempt settlements below 200")
    assert store.get_draft(draft.id).raw_text == "Exempt settlements below 200"
    assert from_device_b.status == DraftStatus.EDITING


def test_draft_does_not_affect_simulation_until_submitted():
    """A draft in Editing state must never appear in pending revisions."""
    store = LawStore()
    store.add_law(Law(id="l1", civilization_id="c1", title="T", raw_text="x", canonical=None))
    store.create_draft("c1", "half-formed idea", target_law_id="l1")
    assert store.pending_revisions("c1") == []


def test_submit_validated_draft_creates_revision():
    store = LawStore()
    store.add_law(Law(id="l1", civilization_id="c1", title="T", raw_text="x", canonical=None))
    draft = store.create_draft("c1", "new text", target_law_id="l1")
    try:
        store.submit_draft(draft.id)
        assert False, "unvalidated draft must not be submittable"
    except LawStoreError:
        pass
    store.update_draft(draft.id, canonical_draft=CanonicalLaw(kind="tax"))
    rev = store.submit_draft(draft.id)
    assert rev.action == LawUserAction.SUBMIT_DRAFT
    assert store.get_draft(draft.id).status == DraftStatus.SUBMITTED
    assert rev.sequence_number == 1


def test_cancel_pending_change_and_discard_draft():
    store = LawStore()
    store.add_law(Law(id="l1", civilization_id="c1", title="T", raw_text="x", canonical=None))
    d = store.create_draft("c1", "trash", None)
    store.discard_draft(d.id)
    assert d not in store.list_drafts("c1")

    from poem.core import CanonicalLawChange
    rev = store.record_revision("l1", "c1", LawUserAction.AMEND_LAW, "oops",
                               CanonicalLawChange(action=LawUserAction.AMEND_LAW))
    cancelled = store.cancel_pending_change(rev.id)
    assert cancelled.status.value == "Superseded"
    assert store.pending_revisions("c1") == []


def test_revision_history_is_preserved():
    store = LawStore()
    store.add_law(Law(id="l1", civilization_id="c1", title="T", raw_text="x", canonical=None))
    from poem.core import CanonicalLawChange
    ids = [store.record_revision("l1", "c1", LawUserAction.CLARIFY_LAW, f"v{i}",
                                 CanonicalLawChange(action=LawUserAction.CLARIFY_LAW)).id
           for i in range(4)]
    hist = store.version_history("l1")
    assert [r.id for r in hist] == ids                       # ordered by sequence
    assert [r.sequence_number for r in hist] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# §15 — API
# ---------------------------------------------------------------------------

def server_with_law():
    srv = WorldServer("w1")
    srv.add_civilization(Civilization(id="c1", progress=0.25))
    r = srv.handle("POST", "/api/v1/laws", {
        "id": "law-tax", "civilization_id": "c1", "title": "Head tax",
        "raw_text": "pay per hearth",
    })
    assert r["status"] == 200
    return srv


def test_api_draft_lifecycle():
    srv = server_with_law()
    created = srv.handle("POST", "/api/v1/laws/drafts",
                         {"civilization_id": "c1", "raw_text": "draft body",
                          "target_law_id": "law-tax"})
    assert created["status"] == 200
    draft_id = created["draft"]["id"]

    upd = srv.handle("PUT", f"/api/v1/laws/drafts/{draft_id}",
                     {"canonical_draft": {"kind": "tax", "scope": {"population_below": 200}}})
    assert upd["draft"]["status"] == "Validated"

    got = srv.handle("GET", f"/api/v1/laws/drafts/{draft_id}")
    assert got["draft"]["id"] == draft_id

    lst = srv.handle("GET", "/api/v1/laws/drafts", {"civilization_id": "c1"})
    assert len(lst["drafts"]) == 1

    deleted = srv.handle("DELETE", f"/api/v1/laws/drafts/{draft_id}")
    assert deleted["status"] == 200


def test_api_revisions_and_pending_view():
    srv = server_with_law()
    cutoff = DAY.replace(hour=20)
    srv.handle("POST", "/api/v1/worlds/w1/turns/open",
               {"started_at": DAY.isoformat(), "simulation_cutoff_at": cutoff.isoformat()})

    early = srv.handle("POST", "/api/v1/laws/law-tax/revisions", {
        "civilization_id": "c1", "action": "AmendLaw", "raw_text": "before cutoff",
        "now": DAY.replace(hour=9).isoformat(),
    })
    late = srv.handle("POST", "/api/v1/laws/law-tax/revisions", {
        "civilization_id": "c1", "action": "AmendLaw", "raw_text": "after cutoff",
        "now": DAY.replace(hour=21).isoformat(),
    })
    assert early["status"] == 200 and late["status"] == 200
    # notice confirms no time-of-day effect (§16.3)
    assert "Время подачи не влияет" in early["notice"] or "следующем ходе" in late["notice"]

    pending = srv.handle("GET", "/api/v1/laws/pending")
    assert len(pending["will_apply_this_slice"]) == 1
    assert len(pending["awaiting_next_turn"]) == 1
    assert "TASK-060" in pending["note"]

    run = srv.handle("POST", "/api/v1/worlds/w1/turns/run", {})
    assert run["status"] == 200
    assert early["revision"]["id"] in run["applied_revisions"]
    assert late["revision"]["id"] in run["deferred_revisions"]

    hist = srv.handle("GET", "/api/v1/laws/law-tax/revisions")
    assert len(hist["revisions"]) == 2


def test_api_chronology_response_shape():
    """§15.4 example response shape."""
    srv = server_with_law()
    resp = srv.handle("GET", "/api/v1/worlds/w1/chronology")
    assert resp["status"] == 200
    for key in ("current_turn", "current_year", "current_season",
                "current_era", "next_turn_span"):
        assert key in resp
    assert set(resp["next_turn_span"]) == {"years", "seasons", "days"}
    # classical era median civ -> 25 years per turn at standard speed
    assert resp["next_turn_span"]["years"] == 25.0


def test_api_treaty_stored_in_game_time():
    srv = server_with_law()
    resp = srv.handle("POST", "/api/v1/treaties",
                      {"world_id": "w1", "civilization_id": "c1",
                       "duration_years": 100, "kind": "peace"})
    assert resp["duration_years"] == 100.0
    # the live effect list is owned by the engine (it rebinds on turn tick)
    eff = srv.engine.timed_effects[0]
    assert eff.remaining.years == 100.0   # game time, not turn count


def test_api_unknown_route_404():
    srv = server_with_law()
    assert srv.handle("GET", "/api/v1/nope")["status"] == 404


def test_pending_buckets_match_section_15_3():
    """§15.3: response must distinguish 'joined the current snapshot' from
    'will be counted in the next slice' and 'awaiting next turn'."""
    srv = server_with_law()
    cutoff = DAY.replace(hour=20)
    srv.handle("POST", "/api/v1/worlds/w1/turns/open",
               {"started_at": DAY.isoformat(), "simulation_cutoff_at": cutoff.isoformat()})
    early = srv.handle("POST", "/api/v1/laws/law-tax/revisions", {
        "civilization_id": "c1", "action": "AmendLaw", "raw_text": "before cutoff",
        "now": DAY.replace(hour=9).isoformat()})["revision"]
    late = srv.handle("POST", "/api/v1/laws/law-tax/revisions", {
        "civilization_id": "c1", "action": "ClarifyLaw", "raw_text": "after cutoff",
        "now": DAY.replace(hour=21).isoformat()})["revision"]

    # before prepare_snapshot: nothing has joined a snapshot yet
    p0 = srv.handle("GET", "/api/v1/laws/pending")
    assert p0["included_in_current_snapshot"] == []
    assert [r["id"] for r in p0["will_apply_this_slice"]] == [early["id"]]
    assert [r["id"] for r in p0["awaiting_next_turn"]] == [late["id"]]

    engine = srv.engine
    engine.prepare_snapshot(now=cutoff)
    p1 = srv.handle("GET", "/api/v1/laws/pending")
    ids_in_snap = [r["id"] for r in p1["included_in_current_snapshot"]]
    assert early["id"] in ids_in_snap          # уже вошла в текущий снимок
    assert late["id"] not in ids_in_snap       # ожидает следующего хода


def test_snapshot_endpoint_exposes_turn_law_snapshot():
    """§4.3: GET /api/v1/laws/snapshot returns exactly what the simulation reads."""
    srv = server_with_law()
    cutoff = DAY.replace(hour=20)
    srv.handle("POST", "/api/v1/worlds/w1/turns/open",
               {"started_at": DAY.isoformat(), "simulation_cutoff_at": cutoff.isoformat()})
    rev = srv.handle("POST", "/api/v1/laws/law-tax/revisions", {
        "civilization_id": "c1", "action": "SuspendLaw",
        "raw_text": "suspend it", "suspend_years": 1.0,
        "now": DAY.replace(hour=10).isoformat()})["revision"]

    srv.engine.prepare_snapshot(now=cutoff)
    snap = srv.handle("GET", "/api/v1/laws/snapshot")
    assert snap["status"] == 200
    civ = snap["civilizations"][0]
    assert civ["civilization_id"] == "c1"
    assert "law-tax" in civ["suspended_laws"]           # suspension recorded (§4.2)
    assert rev["id"] in civ["pending_amendments"]
    assert "law-tax" not in civ["active_laws"]          # not active this turn
    # after the turn finalizes, the snapshot remains inspectable (§15.3)
    run = srv.handle("POST", "/api/v1/worlds/w1/turns/run", {})
    assert run["status"] == 200
    snap2 = srv.handle("GET", "/api/v1/laws/snapshot")
    assert rev["id"] in snap2["civilizations"][0]["pending_amendments"]
