"""Tests for §13 step 13 (norm-conflict detection) & §5.1 contradiction_penalty.

Covers the three recognised contradictions:
 * same canonical kind,
 * contradictory kind pair (tax vs ban),
 * repeal of a law other active laws still depend on (§5.1 «конфликт с
   другими законами»),
plus snapshot-time evaluation against the POST-repeal final norm set (§4.3)
and preview/charge parity without any wall-clock term (§20.5).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.api import WorldServer
from poem.chronology import Civilization, WorldChronology
from poem.core import CanonicalLaw, CanonicalLawChange, Law, LawStatus, LawUserAction
from poem.laws import LawStore
from poem.pricing import PricingConfig, cost_of_law_change
from poem.turn import TurnEngine

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


def make_two_law_world(enforcement_text="Enforced by the Head tax tribunal."):
    store = LawStore()
    civ = Civilization(id="c1", name="Rivers", progress=0.25)  # classical: 25 y/turn
    store.add_law(Law(
        id="law-tax", civilization_id="c1", title="Head tax",
        raw_text="Every settlement pays 1 unit per hearth.",
        canonical=CanonicalLaw(kind="tax"), complexity=1.5, tradition=0.2))
    store.add_law(Law(
        id="law-census", civilization_id="c1", title="Census duty",
        raw_text=f"Count hearths yearly. {enforcement_text}",
        canonical=CanonicalLaw(kind="corvee", refs=("law-tax",)),
        complexity=1.0, tradition=0.1))
    chrono = WorldChronology("w1")
    engine = TurnEngine("w1", store, chrono, [civ])
    return store, chrono, engine, civ


# ---------------------------------------------------------------------------

def test_repeal_of_referenced_law_is_a_norm_conflict():
    """§5.1: repealing 'Head tax' while 'Census duty' depends on it must pay
    contradiction_penalty and record the clash with revision_id (§4.3)."""
    store, chrono, engine, civ = make_two_law_world()
    rev = store.record_revision(
        "law-tax", "c1", LawUserAction.REPEAL_LAW, "Repeal the head tax.",
        CanonicalLawChange(action=LawUserAction.REPEAL_LAW),
        now=DAY.replace(hour=10))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    snaps = engine.prepare_snapshot()
    snap = next(s for s in snaps if s.civilization_id == "c1")

    clash = [c for c in snap.conflicts if c.law_a == "law-tax"]
    assert len(clash) == 1
    assert clash[0].law_b == "law-census"
    assert clash[0].revision_id == rev.id          # UI can point at the edit
    assert clash[0].severity == pytest.approx(0.75)

    cfg = PricingConfig()
    base = cost_of_law_change(store.get_law("law-tax"), LawUserAction.REPEAL_LAW,
                              cfg=cfg, scope_delta=len(rev.raw_text) / 500.0,
                              active_laws=snap.active_laws)
    assert rev.cost == pytest.approx(base + cfg.contradiction_penalty)


def test_repeal_without_dependents_has_no_contradiction_penalty():
    """A cleanly isolated repeal is not punished (§5: price reflects game state)."""
    store, chrono, engine, civ = make_two_law_world(enforcement_text="")
    store.get_law("law-census").canonical.refs = ()
    rev = store.record_revision(
        "law-tax", "c1", LawUserAction.REPEAL_LAW, "Repeal the head tax.",
        CanonicalLawChange(action=LawUserAction.REPEAL_LAW),
        now=DAY.replace(hour=10))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    snaps = engine.prepare_snapshot()
    snap = next(s for s in snaps if s.civilization_id == "c1")
    assert [c for c in snap.conflicts if c.law_a == "law-tax"] == []
    cfg = PricingConfig()
    assert rev.cost == pytest.approx(cost_of_law_change(
        store.get_law("law-tax"), LawUserAction.REPEAL_LAW, cfg=cfg,
        scope_delta=len(rev.raw_text) / 500.0, active_laws=snap.active_laws))


def test_conflicts_are_evaluated_against_the_final_post_repeal_set():
    """§4.3/§13 step 13: an amendment to law-census priced in the SAME slice
    as the repeal of law-tax must NOT count law-tax as a conflicting partner —
    law-tax leaves the active set within this very snapshot."""
    store, chrono, engine, civ = make_two_law_world()
    repeal = store.record_revision(
        "law-tax", "c1", LawUserAction.REPEAL_LAW, "Repeal the head tax.",
        CanonicalLawChange(action=LawUserAction.REPEAL_LAW),
        now=DAY.replace(hour=9))
    amend = store.record_revision(
        "law-census", "c1", LawUserAction.AMEND_LAW, "Amend census wording.",
        CanonicalLawChange(action=LawUserAction.AMEND_LAW,
                           new_canonical=CanonicalLaw(kind="corvee",
                                                      refs=("law-tax",))),
        now=DAY.replace(hour=10))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    snaps = engine.prepare_snapshot()
    snap = next(s for s in snaps if s.civilization_id == "c1")

    # law-tax is repealed this turn => not in active_laws anymore
    assert "law-tax" not in [l.id for l in snap.active_laws]
    # the census amendment therefore has no same-kind/opposed partner left
    census_clashes = [c for c in snap.conflicts if c.law_a == "law-census"]
    assert census_clashes == []
    cfg = PricingConfig()
    assert amend.cost == pytest.approx(cost_of_law_change(
        store.get_law("law-census"), LawUserAction.AMEND_LAW, cfg=cfg,
        scope_delta=len(amend.raw_text) / 500.0, active_laws=snap.active_laws))
    # ...while the repeal itself still sees the dependent census law
    assert any(c.revision_id == repeal.id for c in snap.conflicts)


def test_opposed_kind_pair_conflict_priced_and_charged():
    """tax vs ban is a contradictory pair: amending the ban into tax-like form
    raises cost by contradiction_penalty and charges exactly that amount."""
    store = LawStore()
    civ = Civilization(id="c1", name="Rivers", progress=0.25)
    store.add_law(Law(
        id="law-ban", civilization_id="c1", title="Sea ban",
        raw_text="No fishing beyond the reef.",
        canonical=CanonicalLaw(kind="ban"), complexity=1.0, tradition=0.1))
    store.add_law(Law(
        id="law-fish-tax", civilization_id="c1", title="Fish levy",
        raw_text="Fishers pay one fish per basket.",
        canonical=CanonicalLaw(kind="tax"), complexity=1.0, tradition=0.1))
    engine = TurnEngine("w1", store, WorldChronology("w1"), [civ])
    rev = store.record_revision(
        "law-ban", "c1", LawUserAction.AMEND_LAW, "Lift the ban, tax instead.",
        CanonicalLawChange(action=LawUserAction.AMEND_LAW,
                           new_canonical=CanonicalLaw(kind="tax")),
        now=DAY.replace(hour=12))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    snaps = engine.prepare_snapshot()
    snap = next(s for s in snaps if s.civilization_id == "c1")
    cfg = PricingConfig()
    # the prospective canonical (kind="tax") now overlaps the existing fish
    # levy of the same kind => exactly one contradiction (§5.1)
    clash = [c for c in snap.conflicts if c.law_a == "law-ban" and c.revision_id == rev.id]
    assert len(clash) == 1
    base = cost_of_law_change(store.get_law("law-ban"), LawUserAction.AMEND_LAW,
                              cfg=cfg, scope_delta=len(rev.raw_text) / 500.0,
                              active_laws=snap.active_laws)
    assert rev.cost == pytest.approx(base + cfg.contradiction_penalty)


def test_cost_preview_matches_snapshot_charge_including_repeal_dependency():
    """§16 preview must show the exact number charged later — including the
    repeal-dependency rule — and never depends on submission hour (§20.5)."""
    for hour in (3, 18):
        srv = WorldServer("w-preview")
        srv.add_civilization(Civilization(id="c1", name="Rivers", progress=0.25))
        resp = srv.handle("POST", "/api/v1/laws", {
            "id": "law-tax", "civilization_id": "c1", "title": "Head tax",
            "raw_text": "Every settlement pays 1 unit per hearth.",
            "canonical": {"kind": "tax"}})
        assert resp["status"] == 200
        srv.handle("POST", "/api/v1/laws", {
            "id": "law-census", "civilization_id": "c1", "title": "Census duty",
            "raw_text": "Count hearths yearly. Enforced by the Head tax tribunal.",
            "canonical": {"kind": "corvee"}})
        prev = srv.handle("POST", "/api/v1/laws/law-tax/cost_preview", {
            "action": "repeal_law", "raw_text": "Repeal the head tax."})
        assert prev["contradictions"] == 1, "preview must see the dependency clash"

        created = srv.handle("POST", "/api/v1/laws/law-tax/revisions", {
            "civilization_id": "c1",
            "action": "repeal_law",
            "raw_text": "Repeal the head tax.",
            "canonical_change": {"action": "repeal_law"},
            "now": f"2026-10-10T{hour:02d}:00:00+00:00"})
        assert created["status"] == 200
        run = srv.handle("POST", "/api/v1/worlds/w-preview/turns/run", {
            "started_at": "2026-10-10T00:00:00+00:00",
            "simulation_cutoff_at": "2026-10-10T23:00:00+00:00"})
        assert run["status"] == 200, run.get("error")
        charged = run["admin_costs"].get("c1", 0.0)
        assert charged == pytest.approx(prev["estimated_cost"]), \
            "preview/charge drift detected"
        # ...and the number is identical whatever the submission hour was
        globals().setdefault("_charged_by_hour", {})[hour] = charged
    assert _charged_by_hour[3] == _charged_by_hour[18]
