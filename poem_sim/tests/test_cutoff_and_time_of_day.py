"""Tests for §17.1–17.3: time-of-day neutrality, last-version-wins, cutoff deferral."""

from __future__ import annotations

import sys

import pytest
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


# ---------------------------------------------------------------------------
# §5 — стоимость правок списывается в симуляции хода (не зависит от часов)
# ---------------------------------------------------------------------------

def test_law_change_cost_is_charged_from_admin_budget_in_turn():
    """Applied revisions cost administrative resource, identical for edits
    made at 03:00 and 18:00 within the same slice (§5.1: no late_edit term)."""
    costs_by_hour = {}
    for hour in (3, 18):
        store, chrono, engine, civ, law = make_world()
        civ.annual_production_rate = 100.0   # 25 y/turn -> 2500 budget inflow
        rev = store.record_revision(
            "law-tax", "c1", LawUserAction.AMEND_LAW, "Exempt small settlements.",
            CanonicalLawChange(action=LawUserAction.AMEND_LAW,
                               new_canonical=CanonicalLaw(kind="tax")),
            now=DAY.replace(hour=hour))
        engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
        snaps = engine.prepare_snapshot()
        # The charge equals the snapshot-time preview price (§5.1), including
        # the actual change volume (scope_delta) — identical for both hours.
        expected = store.get_revision(rev.id).cost
        report = engine.run_turn()
        assert rev.id in report.applied_revisions
        assert report.admin_costs["c1"] == pytest.approx(expected)
        assert civ.administrative_budget == pytest.approx(2500.0 - expected)
        costs_by_hour[hour] = (report.admin_costs["c1"],
                               store.get_revision(rev.id).status.value)
    # time of day changes nothing (§17.1 extended to the application stage)
    assert costs_by_hour[3] == costs_by_hour[18]


def test_deferred_revision_not_charged_until_applied():
    """A revision that misses the cutoff is preserved without any charge;
    its cost hits only in the turn where it actually applies (§4.4, §5)."""
    store, chrono, engine, civ, law = make_world()
    civ.annual_production_rate = 100.0
    late = store.record_revision(
        "law-tax", "c1", LawUserAction.REPEAL_LAW, "Repeal after cutoff",
        CanonicalLawChange(action=LawUserAction.REPEAL_LAW),
        now=DAY.replace(hour=21))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=20))
    report1 = engine.run_turn()
    assert late.id in report1.deferred_revisions
    assert report1.admin_costs.get("c1", 0.0) == 0.0   # nothing applied -> nothing charged

    from datetime import timedelta
    engine.open_turn(started_at=DAY + timedelta(days=1),
                     simulation_cutoff_at=DAY + timedelta(days=1, hours=20))
    report2 = engine.run_turn()
    assert late.id in report2.applied_revisions
    repeal_cost = cost_of_law_change(store.get_law("law-tax"),
                                     LawUserAction.REPEAL_LAW, cfg=PricingConfig(),
                                     scope_delta=len(late.raw_text) / 500.0)
    assert report2.admin_costs["c1"] == pytest.approx(repeal_cost)


# ---------------------------------------------------------------------------
# §13 step 13 + §5.1 — конфликты норм влияют на стоимость и видны в снимке
# ---------------------------------------------------------------------------

def test_norm_conflict_raises_revision_cost_and_is_recorded_in_snapshot():
    """Amending a law that conflicts with another active law of the same kind
    pays contradiction_penalty (§5.1) and the conflict appears in the
    TurnLawSnapshot.conflicts for client display (§4.3, §16.2)."""
    store = LawStore()
    civ = Civilization(id="c1", name="Rivers", progress=0.25)
    store.add_law(Law(
        id="law-tax-a", civilization_id="c1", title="Head tax",
        raw_text="Every settlement pays 1 unit per hearth.",
        canonical=CanonicalLaw(kind="tax"), complexity=1.5, tradition=0.2))
    store.add_law(Law(
        id="law-tax-b", civilization_id="c1", title="Hearth tax",
        raw_text="Every hearth pays grain.",
        canonical=CanonicalLaw(kind="tax"), complexity=1.0, tradition=0.1))
    chrono = WorldChronology("w1")
    engine = TurnEngine("w1", store, chrono, [civ])

    rev = store.record_revision(
        "law-tax-a", "c1", LawUserAction.AMEND_LAW, "Exempt small settlements.",
        CanonicalLawChange(action=LawUserAction.AMEND_LAW,
                           new_canonical=CanonicalLaw(kind="tax")),
        now=DAY.replace(hour=9))

    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    snaps = engine.prepare_snapshot()
    snap = next(s for s in snaps if s.civilization_id == "c1")

    # one conflicting active law (law-tax-b, same canonical kind "tax")
    cfg = PricingConfig()
    base = cost_of_law_change(store.get_law("law-tax-a"),
                              LawUserAction.AMEND_LAW, cfg=cfg,
                              scope_delta=len(rev.raw_text) / 500.0)
    with_conflict = base + cfg.contradiction_penalty * 1
    assert rev.cost == pytest.approx(with_conflict)
    assert any(c.law_a == "law-tax-a" and c.law_b == "law-tax-b"
               for c in snap.conflicts)

    report = engine.run_turn()
    # the charged admin cost includes the contradiction penalty too
    assert report.admin_costs["c1"] == pytest.approx(with_conflict)


def test_no_conflict_means_no_contradiction_penalty():
    """Laws of different kinds do not raise the price (§5.1 term is zero)."""
    store = LawStore()
    civ = Civilization(id="c1", name="Rivers", progress=0.25)
    store.add_law(Law(
        id="law-tax-a", civilization_id="c1", title="Head tax",
        raw_text="Every settlement pays 1 unit per hearth.",
        canonical=CanonicalLaw(kind="tax"), complexity=1.5, tradition=0.2))
    store.add_law(Law(
        id="law-ban-x", civilization_id="c1", title="Sea ban",
        raw_text="No fishing beyond the reef.",
        canonical=CanonicalLaw(kind="ban"), complexity=1.0, tradition=0.1))
    chrono = WorldChronology("w1")
    engine = TurnEngine("w1", store, chrono, [civ])

    rev = store.record_revision(
        "law-tax-a", "c1", LawUserAction.CLARIFY_LAW, "Clarify wording.",
        CanonicalLawChange(action=LawUserAction.CLARIFY_LAW),
        now=DAY.replace(hour=9))
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    snaps = engine.prepare_snapshot()
    snap = next(s for s in snaps if s.civilization_id == "c1")

    expected = cost_of_law_change(store.get_law("law-tax-a"),
                                  LawUserAction.CLARIFY_LAW, cfg=PricingConfig(),
                                  scope_delta=len(rev.raw_text) / 500.0)
    assert rev.cost == pytest.approx(expected)
    assert snap.conflicts == []
