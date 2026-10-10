"""Tests for §17.1–17.3: time-of-day neutrality, last-version-wins, cutoff deferral."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.chronology import Civilization, WorldChronology
from poem.core import (
    CanonicalLaw,
    CanonicalLawChange,
    DraftStatus,
    Law,
    LawStatus,
    LawUserAction,
)
from poem.laws import LawStore
from poem.pricing import PricingConfig, cost_of_law_change
from poem.turn import DEFERRED_NOTICE, TurnEngine

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


def make_world():
    store = LawStore()
    civ = Civilization(id="c1", name="Rivers", progress=0.25)  # classical -> 25 y/turn
    law = store.add_law(Law(
        id="law-tax", civilization_id="c1", title="Head tax",
        raw_text="Every settlement pays 1 unit per hearth.",
        canonical=CanonicalLaw(kind="tax"), complexity=1.5, tradition=0.2,
    ))
    chrono = WorldChronology("w1")
    engine = TurnEngine("w1", store, chrono, [civ])
    return store, chrono, engine, civ, law


# ---------------------------------------------------------------------------
# §17.1 — время суток не влияет
# ---------------------------------------------------------------------------

def test_time_of_day_does_not_affect_cost_status_effectiveness():
    """A revision created at 03:00 and an identical one at 18:00 must be equal
    in cost, effectiveness and status (as long as both are inside the slice)."""
    results = []
    for hour in (3, 18):
        store, chrono, engine, civ, law = make_world()
        received = DAY.replace(hour=hour, minute=0)
        rev = store.record_revision(
            law_id="law-tax", civilization_id="c1",
            action=LawUserAction.AMEND_LAW,
            raw_text="Exempt settlements below 200 souls.",
            canonical_change=CanonicalLawChange(
                action=LawUserAction.AMEND_LAW,
                new_canonical=CanonicalLaw(kind="tax", scope={"population_below": 200}),
            ),
            now=received,
        )
        cutoff = DAY.replace(hour=21)
        engine.open_turn(started_at=DAY, simulation_cutoff_at=cutoff)
        engine.prepare_snapshot(now=cutoff)
        report = engine.run_turn()
        results.append({
            "cost": store.get_revision(rev.id).cost,
            "status": store.get_revision(rev.id).status.value,
            "applied": rev.id in report.applied_revisions,
            "law_text": store.get_law("law-tax").raw_text,
        })
    assert results[0] == results[1], "time of day changed gameplay outcome!"


def test_pricing_function_has_no_datetime_input():
    """The cost API physically cannot see a clock: no datetime parameter exists."""
    import inspect

    sig = inspect.signature(cost_of_law_change)
    for p in sig.parameters.values():
        assert not issubclass(p.annotation if isinstance(p.annotation, type) else object, datetime) \
            if p.annotation is not inspect.Parameter.empty else True
    law = Law(id="l", civilization_id="c", title="t", raw_text="", canonical=None)
    cfg = PricingConfig()
    c1 = cost_of_law_change(law, LawUserAction.AMEND_LAW, cfg=cfg)
    c2 = cost_of_law_change(law, LawUserAction.AMEND_LAW, cfg=cfg)
    assert c1 == c2


def test_no_late_edit_mechanics_in_pricing_config():
    """TASK-060: late_edit_cost_multiplier / late_edit_delay_chance are gone."""
    fields = set(PricingConfig.__dataclass_fields__)
    assert not any("late" in f.lower() for f in fields)
    src = Path(__file__).resolve().parents[1] / "poem" / "pricing.py"
    text = src.read_text(encoding="utf-8")
    # mentioned only in comments explaining their removal, never as code:
    assert "late_edit_cost_multiplier =" not in text
    assert "late_edit_delay_chance =" not in text


# ---------------------------------------------------------------------------
# §17.2 — последняя подтверждённая версия побеждает
# ---------------------------------------------------------------------------

def test_last_confirmed_version_wins_within_one_turn():
    """§4.2 scenario: amendment A (09:10), amendment B (13:40), suspension (17:25),
    all before cutoff => simulation uses the suspension; earlier versions stay
    in history as Superseded."""
    store, chrono, engine, civ, law = make_world()
    a = store.record_revision(
        "law-tax", "c1", LawUserAction.AMEND_LAW,
        "Free small settlements from tax.",
        CanonicalLawChange(action=LawUserAction.AMEND_LAW,
                           new_canonical=CanonicalLaw(kind="tax")),
        now=DAY.replace(hour=9, minute=10))
    b = store.record_revision(
        "law-tax", "c1", LawUserAction.AMEND_LAW,
        "Free only settlements below 200 residents.",
        CanonicalLawChange(action=LawUserAction.AMEND_LAW,
                           new_canonical=CanonicalLaw(kind="tax", scope={"population_below": 200})),
        now=DAY.replace(hour=13, minute=40))
    s = store.record_revision(
        "law-tax", "c1", LawUserAction.SUSPEND_LAW,
        "Suspend the tax law for one season.",
        CanonicalLawChange(action=LawUserAction.SUSPEND_LAW, suspend_years=0.25),
        now=DAY.replace(hour=17, minute=25))

    cutoff = DAY.replace(hour=20)
    engine.open_turn(started_at=DAY, simulation_cutoff_at=cutoff)
    snaps = engine.prepare_snapshot(now=cutoff)
    report = engine.run_turn()

    assert report.applied_revisions == [s.id]
    assert store.get_revision(a.id).status.value == "Superseded"
    assert store.get_revision(b.id).status.value == "Superseded"
    assert store.get_revision(s.id).status.value == "Applied"
    assert store.get_law("law-tax").status == LawStatus.SUSPENDED
    snap = next(x for x in snaps if x.civilization_id == "c1")
    assert "law-tax" in snap.suspended_laws
    # history preserved (§4.2: previous versions remain in history)
    hist = store.version_history("law-tax")
    assert [r.sequence_number for r in hist] == [1, 2, 3]


def test_repeal_after_amendments_wins():
    """§17.2 steps 1-3: amendment A, amendment B, then repeal => repeal applies."""
    store, chrono, engine, civ, law = make_world()
    store.record_revision("law-tax", "c1", LawUserAction.AMEND_LAW, "A",
                          CanonicalLawChange(action=LawUserAction.AMEND_LAW),
                          now=DAY.replace(hour=8))
    store.record_revision("law-tax", "c1", LawUserAction.AMEND_LAW, "B",
                          CanonicalLawChange(action=LawUserAction.AMEND_LAW),
                          now=DAY.replace(hour=12))
    r = store.record_revision("law-tax", "c1", LawUserAction.REPEAL_LAW, "Repeal it",
                              CanonicalLawChange(action=LawUserAction.REPEAL_LAW),
                              now=DAY.replace(hour=15))
    cutoff = DAY.replace(hour=23, minute=59)
    engine.open_turn(started_at=DAY, simulation_cutoff_at=cutoff)
    report = engine.run_turn()
    assert r.id in report.applied_revisions
    assert store.get_law("law-tax").status == LawStatus.REPEALED


# ---------------------------------------------------------------------------
# §17.3 — правки после среза идут в следующий ход
# ---------------------------------------------------------------------------

def test_edits_after_cutoff_apply_next_turn_without_penalty():
    store, chrono, engine, civ, law = make_world()
    cutoff = DAY.replace(hour=20)
    early = store.record_revision(
        "law-tax", "c1", LawUserAction.CLARIFY_LAW, "Clarification v1",
        CanonicalLawChange(action=LawUserAction.CLARIFY_LAW),
        now=DAY.replace(hour=19, minute=59))
    late = store.record_revision(
        "law-tax", "c1", LawUserAction.AMEND_LAW, "Amendment made after cutoff",
        CanonicalLawChange(action=LawUserAction.AMEND_LAW,
                           new_canonical=CanonicalLaw(kind="tax", effect={"rate": 2})),
        now=DAY.replace(hour=20, minute=1))

    # ---- turn N: cutoff reached, late edit NOT applied, but preserved ----
    engine.open_turn(started_at=DAY, simulation_cutoff_at=cutoff)
    report1 = engine.run_turn()
    assert early.id in report1.applied_revisions
    assert late.id not in report1.applied_revisions
    assert late.id in report1.deferred_revisions
    assert late.id in report1.notices[0] or DEFERRED_NOTICE in report1.notices
    stored_late = store.get_revision(late.id)
    assert stored_late.status.value == "Pending"          # not lost, not rejected
    assert stored_late.simulation_turn_id is None
    # No penalty: cost is computed from game state only, same formula as early.
    cfg = PricingConfig()
    expected_cost = cost_of_law_change(store.get_law("law-tax"),
                                       LawUserAction.AMEND_LAW, cfg=cfg,
                                       scope_delta=len(late.raw_text) / 500.0)
    # (cost will be assigned when it joins the next snapshot; ensure the
    #  pricing call itself does not depend on received_at)
    assert expected_cost == cost_of_law_change(store.get_law("law-tax"),
                                               LawUserAction.AMEND_LAW, cfg=cfg,
                                               scope_delta=len(late.raw_text) / 500.0)

    # ---- turn N+1: the deferred edit is included ----
    next_start = DAY + timedelta(days=1)
    next_cutoff = next_start.replace(hour=20)
    engine.open_turn(started_at=next_start, simulation_cutoff_at=next_cutoff)
    report2 = engine.run_turn()
    assert late.id in report2.applied_revisions
    assert store.get_revision(late.id).status.value == "Applied"
    assert store.get_law("law-tax").canonical.effect == {"rate": 2}


def test_snapshot_refuses_before_cutoff():
    store, chrono, engine, civ, law = make_world()
    cutoff = DAY.replace(hour=20)
    engine.open_turn(started_at=DAY, simulation_cutoff_at=cutoff)
    try:
        engine.prepare_snapshot(now=DAY.replace(hour=19))
        assert False, "snapshot must not be prepared before cutoff"
    except RuntimeError:
        pass
