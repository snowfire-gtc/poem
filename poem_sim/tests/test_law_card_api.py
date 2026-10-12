"""§16.2 — GET /api/v1/laws/{id}: full client card for a law.

The card must show: current version, drafts, pending edits (with the slice
they will join), version history, transition periods with remaining time in
GAME units, tradition, norm conflicts and folklore effects.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.api import WorldServer
from poem.chronology import Civilization
from poem.core import (
    FolkloreItem,
)

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


def make_server():
    srv = WorldServer("w1")
    srv.add_civilization(Civilization(id="c1", name="Rivers", progress=0.25))
    r = srv.handle("POST", "/api/v1/laws", {
        "id": "law-tax", "civilization_id": "c1", "title": "Head tax",
        "raw_text": "Every settlement pays 1 unit per hearth.",
        "canonical": {"kind": "tax"}, "complexity": 1.5,
    })
    assert r["status"] == 200
    return srv


def test_law_card_lists_drafts_pending_history_conflicts_folklore():
    srv = make_server()
    # second same-kind active law -> conflict on the card (§4.3/§16.2)
    srv.handle("POST", "/api/v1/laws", {
        "id": "law-hearth", "civilization_id": "c1", "title": "Hearth tax",
        "raw_text": "Grain per hearth.", "canonical": {"kind": "tax"},
    })
    # draft targeting the law (§3.3)
    d = srv.handle("POST", "/api/v1/laws/drafts", {
        "civilization_id": "c1", "target_law_id": "law-tax",
        "raw_text": "Exempt small settlements…",
    })
    # pending revision (§15.2)
    srv.handle("POST", "/api/v1/laws/law-tax/revisions", {
        "civilization_id": "c1",
        "action": "AmendLaw", "raw_text": "Exempt below 200 souls.",
        "canonical_change": {"new_canonical": {"kind": "tax", "scope": {"population_below": 200}}},
        "now": DAY.replace(hour=9).isoformat(),
    })
    # folklore referencing the law topic (§11.5)
    srv.engine.folklore.append(FolkloreItem(
        id="f1", civilization_id="c1", title="Song of the Taxman",
        topic="law-tax", oral_strength=0.6))

    # open a turn so that `will_apply_in` can be evaluated against the cutoff
    srv.handle("POST", "/api/v1/worlds/w1/turns/open", {
        "started_at": DAY.isoformat(),
        "simulation_cutoff_at": DAY.replace(hour=23).isoformat(),
    })

    card = srv.handle("GET", "/api/v1/laws/law-tax")
    assert card["status"] == 200
    body = card

    assert body["law"]["id"] == "law-tax"                # current version
    assert any(x["id"] == d["draft"]["id"] for x in body["drafts"])   # drafts
    pend = body["pending_edits"]
    assert len(pend) == 1 and pend[0]["will_apply_in"] == "current_turn"
    assert len(body["version_history"]) == 1             # history (§4.2)
    conf = body["conflicts"]
    assert any(c["law_b"] == "law-hearth" for c in conf)  # norm conflict
    assert any(f["id"] == "f1" for f in body["folklore_effects"])
    assert "tradition" in body["law"]                    # tradition shown


def test_law_card_shows_transition_remaining_in_game_years():
    """§16.2 'остаток времени переходного периода' — in GAME time, not turns."""
    srv = make_server()
    srv.handle("POST", "/api/v1/laws/law-tax/revisions", {
        "civilization_id": "c1",
        "action": "AmendLaw", "raw_text": "New levy.",
        "canonical_change": {"new_canonical": {"kind": "tax"},
                             "transition": {"years": 10.0}},
        "now": DAY.replace(hour=9).isoformat(),
    })
    srv.handle("POST", "/api/v1/worlds/w1/turns/open", {
        "started_at": DAY.isoformat(),
        "simulation_cutoff_at": DAY.replace(hour=23).isoformat(),
    })
    run = srv.handle("POST", "/api/v1/worlds/w1/turns/run", {})
    assert run["status"] == 200

    card = srv.handle("GET", "/api/v1/laws/law-tax")
    tr = [t for t in card["transitions"] if t["kind"] == "law_transition"]
    assert len(tr) == 1
    # classical era span is 25 y; a 10-year transition created THIS turn was
    # not ticked yet (§11.7 guard) => exactly 10 game years remain
    assert tr[0]["remaining_years"] == 10.0


def test_law_card_pending_after_cutoff_says_next_turn():
    """§16.3: the card tells the player which turn the edit lands in — no
    time-of-day penalty wording anywhere."""
    srv = make_server()
    late = datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc)
    srv.handle("POST", "/api/v1/worlds/w1/turns/open", {
        "started_at": DAY.isoformat(),
        "simulation_cutoff_at": DAY.replace(hour=20).isoformat(),
    })
    srv.handle("POST", "/api/v1/laws/law-tax/revisions", {
        "civilization_id": "c1",
        "action": "ClarifyLaw", "raw_text": "Clarified after cutoff.",
        "now": late.isoformat(),
    })
    card = srv.handle("GET", "/api/v1/laws/law-tax")
    pend = card["pending_edits"]
    assert len(pend) == 1
    assert pend[0]["will_apply_in"] == "next_turn"
    # stored server-side, nothing lost (§20 rule 6)
    assert pend[0]["status"] == "Pending"


def test_law_card_404_like_error_for_unknown_law():
    srv = make_server()
    resp = srv.handle("GET", "/api/v1/laws/nope")
    assert resp["status"] == 400  # validation errors surface as 4xx
