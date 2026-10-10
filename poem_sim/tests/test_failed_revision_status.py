"""§3.4 status ``Failed`` — the last unreachable revision verdict is now real.

A revision that joined the simulation slice but whose application raised is:
 * marked Failed (kept in version history for audit, §16.2);
 * NOT charged from the administrative budget (§5: pay only what happens);
 * reported to the player via notices (§16.3) and GET /laws/pending (§15.3
   bucket «failed»);
 * never silently retried in later turns;
 * a failed repeal leaves no customary remnant in folk memory (§11.5).

Verdicts are time-of-day neutral: identical failure behaviour at 03:00 and
18:00 (§17.1 extended to the Failed path).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.api import WorldServer
from poem.chronology import Civilization, WorldChronology
from poem.core import (
    CanonicalLaw,
    CanonicalLawChange,
    Law,
    LawStatus,
    LawUserAction,
)
from poem.laws import LawStore
from poem.turn import TurnEngine

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


class _BrokenStore(LawStore):
    """Simulates an application-time error for one specific law.

    The fault fires only while the engine is *applying* revisions (the
    ``fault_phase`` flag is raised by the test right before that phase);
    snapshot/pricing and post-turn audit reads keep working, so tests can
    still inspect the resulting state.
    """

    break_for_law: str = ""
    fault_phase: bool = False

    def get_law(self, law_id):
        if self.fault_phase and law_id == self.break_for_law:
            raise RuntimeError("storage fault during application")
        return super().get_law(law_id)


def build(broken=True):
    store = _BrokenStore() if broken else LawStore()
    if broken:
        store.break_for_law = "law-tax"
    store.add_law(Law(
        id="law-tax", civilization_id="c1", title="Head tax",
        raw_text="Every settlement pays 1 unit per hearth.",
        canonical=CanonicalLaw(kind="tax"), complexity=1.5, tradition=0.2))
    civ = Civilization(id="c1", name="Rivers", progress=0.25)
    civ.annual_production_rate = 100.0
    engine = TurnEngine("w1", store, WorldChronology("w1"), [civ])

    # Arm the fault exactly around the application step (§13 steps 7+12):
    # _apply_snapshot_changes runs with fault_phase on, so get_law raises
    # there and nowhere else (snapshot preparation stays clean).
    orig_apply = engine._apply_snapshot_changes

    def apply_once(snap, applied, failed=None):
        store.fault_phase = True
        try:
            orig_apply(snap, applied, failed)
        finally:
            store.fault_phase = False

    engine._apply_snapshot_changes = apply_once
    return store, engine, civ


# ---------------------------------------------------------------------------

def test_failed_revision_is_marked_failed_not_applied_and_not_charged():
    store, engine, civ = build(broken=True)
    rev = store.record_revision(
        "law-tax", "c1", LawUserAction.REPEAL_LAW, "Repeal the head tax",
        CanonicalLawChange(action=LawUserAction.REPEAL_LAW),
        now=DAY.replace(hour=10))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=20))
    store.armed = True          # fault fires only during application (§3.4)
    report = engine.run_turn()

    assert rev.id not in report.applied_revisions
    assert report.failed_revisions[rev.id] == "storage fault during application"
    assert store.get_revision(rev.id).status.value == "Failed"
    # kept in version history for audit (§16.2)
    assert rev.id in [r.id for r in store.version_history("law-tax")]
    # nothing charged (§5): a change that did not happen is not paid for
    assert report.admin_costs.get("c1", 0.0) == 0.0
    assert civ.administrative_budget == 2500.0   # untouched production inflow
    # player is informed explicitly (§16.3)
    assert any("Failed" in n for n in report.notices)
    # the law itself was NOT modified by the failed application
    assert store.get_law("law-tax").status == LawStatus.ACTIVE
    assert report.expired_effects == []


def test_failed_repeal_leaves_no_folk_remnant_and_is_not_retried():
    store, engine, civ = build(broken=True)
    rev = store.record_revision(
        "law-tax", "c1", LawUserAction.REPEAL_LAW, "Repeal the head tax",
        CanonicalLawChange(action=LawUserAction.REPEAL_LAW),
        now=DAY.replace(hour=10))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=20))
    store.armed = True
    r1 = engine.run_turn()
    assert r1.failed_revisions
    # the repeal never happened -> no customary remnant in folk memory (§11.5)
    assert store.get_law("law-tax").status == LawStatus.ACTIVE
    snap = next(s for s in r1.snapshots if s.civilization_id == "c1")
    assert all(c.id != "law-tax" for c in snap.customary_remnants)

    # Next turn: the Failed revision must NOT re-enter the snapshot (§3.4:
    # never silently retried).
    from datetime import timedelta
    engine.open_turn(started_at=DAY + timedelta(days=1),
                     simulation_cutoff_at=DAY + timedelta(days=1, hours=20))
    r2 = engine.run_turn()
    assert rev.id not in r2.applied_revisions
    assert rev.id not in r2.deferred_revisions
    assert store.get_revision(rev.id).status.value == "Failed"


def test_time_of_day_neutral_failure():
    """Same failure outcome at 03:00 and 18:00 (§17.1 extended)."""
    outcomes = {}
    for hour in (3, 18):
        store, engine, civ = build(broken=True)
        rev = store.record_revision(
            "law-tax", "c1", LawUserAction.AMEND_LAW, "Exempt small settlements",
            CanonicalLawChange(action=LawUserAction.AMEND_LAW,
                               new_canonical=CanonicalLaw(kind="tax")),
            now=DAY.replace(hour=hour))
        engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
        store.armed = True
        report = engine.run_turn()
        outcomes[hour] = (store.get_revision(rev.id).status.value,
                          report.admin_costs.get("c1", 0.0),
                          tuple(report.notices))
    assert outcomes[3] == outcomes[18]


def test_pending_api_reports_failed_bucket():
    """§15.3: GET /laws/pending shows failed revisions separately."""
    srv = WorldServer("w1")
    srv.add_civilization(Civilization(id="c1", name="Rivers", progress=0.25))
    srv.store.add_law(Law(
        id="law-x", civilization_id="c1", title="X", raw_text="t",
        canonical=CanonicalLaw(kind="tax")))
    rev = srv.store.record_revision(
        "law-x", "c1", LawUserAction.CLARIFY_LAW, "Clarify",
        CanonicalLawChange(action=LawUserAction.CLARIFY_LAW),
        now=DAY.replace(hour=9))
    # Drive it into Failed through the store API (audit verdict)...
    srv.store.mark_included(rev.id, "t1")
    srv.store.mark_failed(rev.id, reason="application fault")
    pend = srv.handle("GET", "/api/v1/laws/pending")
    assert pend["status"] == 200
    assert rev.id in [r["id"] for r in pend["failed"]]
    assert rev.id not in [r["id"] for r in pend["will_apply_this_slice"]]
    assert rev.id not in [r["id"] for r in pend["rejected"]]
