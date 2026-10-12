#!/usr/bin/env python3
"""Rev 4 constant calibration via long auto-simulations (§22.10, todo.md Этап 4).

Runs headless worlds of N turns with the political layer ENABLED and reports
the three quantities whose defaults must be validated before release:

  * conspiracy_rate   — overthrows per civilization per 100 game-years
                        (calibrates ``politics.conspiracy.threshold``, §22.10
                        risk «переворот отбирает прогресс»)
  * distortion_depth  — mean share of chronicle items rewritten/buried by the
                        court chronicler (calibrates ``politics.report.*``;
                        §22.10 risk «искажённые отчёты делают игру невозможной»)
  * risk_half_life    — game-years until risk_premium halves on a calm world
                        (analytic check of ``equilibrium.tau_law``: t½ = ln2·τ;
                        §22.3 / TASK-073)

Determinism: every world is seeded, so re-running the same command reproduces
identical numbers (§22.1 rule 1). The tool mutates nothing outside the JSON
report it optionally writes.

Usage:
    python tools/calibrate_politics.py [--turns 400] [--worlds 8]
        [--threshold 0.35] [--flattery 0.5] [--censorship 0.6]
        [--tau-law 80] [--json out.json]

Exit code is always 0 (telemetry, not an assertion gate); values outside the
target bands printed at the bottom are warnings — adjust config/politics.toml
accordingly, then commit both together.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.api import WorldServer
from poem.chronology import Civilization
from poem.core import CanonicalLaw, Law
from poem.politics import PoliticsConfig, load_politics_config

DAY = datetime(2026, 10, 13, tzinfo=timezone.utc)

# Target bands from §22.10 design intent (warnings only):
# a revolt roughly once per human generation per civ, distortion noticeable
# but never drowning the truth, premium halving within a working lifetime.
BAND_CONSPIRACY_PER_100Y = (0.3, 2.5)     # overthrows / civ / 100 years
BAND_DISTORTION = (0.05, 0.60)            # share of new chronicle items
BAND_RISK_HALFLIFE_YEARS = (20.0, 200.0)  # ln2 · tau_law


def build_config(args) -> PoliticsConfig:
    """Base politics.toml overlaid with CLI knobs under calibration."""
    cfg = load_politics_config(Path(__file__).resolve().parents[1]
                               / "config" / "politics.toml")
    cfg.enabled = True
    # Fortune stays ON here: shocks feed grievance like in real games, but
    # capped so one plague storm cannot dominate the conspiracy statistics.
    cfg.fortune.p_min = 0.01
    cfg.fortune.p_max = 0.25
    if args.threshold is not None:
        cfg.conspiracy.threshold = args.threshold
    if args.flattery is not None:
        cfg.report.flattery_scale = args.flattery
    if args.censorship is not None:
        cfg.report.censorship_level = args.censorship
    if args.tau_law is not None:
        cfg.equilibrium.tau_law = args.tau_law
    return cfg


def make_server(world_no: int, cfg: PoliticsConfig,
                ruler_style: float | None = None) -> WorldServer:
    srv = WorldServer(f"calib-{world_no}", politics=cfg,
                      world_seed=1000 + world_no)
    # two rival civilizations with distinct economies ⇒ distinct elite mixes
    srv.add_civilization(Civilization(
        id="cA", name="Аркадия", progress=0.25,
        population=50_000.0, annual_production_rate=900.0,
        annual_research_rate=3.0, administrative_budget=400.0,
        economy_profile={"agriculture": 0.5, "trade": 0.3, "military": 0.2}))
    srv.add_civilization(Civilization(
        id="cB", name="Борея", progress=0.22,
        population=40_000.0, annual_production_rate=700.0,
        annual_research_rate=2.0, administrative_budget=350.0,
        economy_profile={"landed": 0.4, "clergy": 0.3, "military": 0.3}))
    srv.store.add_law(Law(
        id="basic-tax", civilization_id="cA", title="Десятина",
        raw_text="Десятина с зерна",
        canonical=CanonicalLaw(kind="tax", text_summary="Десятина с зерна")))
    if ruler_style is not None:
        # §22.10 calibration knob: distortion depth is a function of the
        # ruler's tyranny (style > 0); with the default neutral incumbent
        # (style 0.0) the court never flatters and the metric measures
        # nothing. Seed both thrones so the report.* constants are probed
        # under an actually tyrannic regime.
        from poem.core import RulerState
        for cid in ("cA", "cB"):
            srv.engine.civilizations[cid].ruler = RulerState(
                name=f"Правитель {srv.engine.civilizations[cid].name}",
                style=ruler_style)
    return srv


def amend(srv: WorldServer, turn_no: int, when: datetime) -> None:
    """One official amendment revision through the public API path (§15.2)."""
    srv.create_revision("laws/basic-tax", {
        "civilization_id": "cA",
        "action": "amend_law",
        "raw_text": f"Десятина с зерна, редакция {turn_no}",
        "canonical_change": {"new_canonical": {
            "kind": "tax",
            "text_summary": f"Десятина с зерна, редакция {turn_no}"}},
        "received_at": when.isoformat(),
    })


def run_world(srv: WorldServer, turns: int) -> dict:
    """Auto-play with alternating regimes so all equilibrium mechanics engage.

    Regime A (first half): heavy decreeing → tax pressure, grievance, plots.
    Regime B (second half): wu wei — no edits at all → dao accumulates,
    aggregates revert, risk premium decays. This mirrors the stress pattern
    §22.10 asks us to measure against.
    """
    overthrows = 0
    conspiracies = 0
    distorted_items = 0
    political_items = 0
    suppressed_total = 0
    risk_trace: list[tuple[float, float]] = []   # (stability_years, premium)
    year = 0.0

    for t in range(turns):
        started = DAY + timedelta(days=t)
        srv.engine.open_turn(
            started_at=started,
            simulation_cutoff_at=started + timedelta(hours=23),
            seed=srv.engine.rng.seed + t)
        if t < turns // 2 and t % 2 == 0:
            amend(srv, t, started + timedelta(hours=1))
        report = srv.engine.run_turn()
        year = report.world_year_after
        overthrows += sum(1 for c in report.conspiracies
                          if c.get("outcome") == "ruler_overthrown")
        conspiracies += len(report.conspiracies)
        # distortion depth (§22.10): share of THIS turn's NEW chronicle items
        # the court chronicler touched — softened/flattered (audit "fields")
        # or buried by censorship (they resurface later, §22.5). Denominator
        # is every new narrative item, taken from hidden_audit so the count
        # stays exact even when censorship removes entries from the public
        # copy. NOTE: the previous formula divided only Fortune/Conspiracy/
        # resurfaced items — rare kinds — by the touched count, which made
        # the ratio exceed 1 and tripped the band warning (§22.10).
        # BUGFIX (calibration run 2026-10-13): `hidden_audit["canonical_events"]`
        # is the WHOLE chronicle of the turn (interval events included), while
        # the chronicler only processes items appended after `canon_start`
        # (turn.py step 19a). Counting the full list as the denominator made
        # depth nearly insensitive to report.* constants (halving them moved
        # the metric by 0.006 — impossible for a real share). The engine now
        # exposes the exact split in narrative_distortion: "new_canonical" is
        # every NEW item, "fields"/"suppressed_count" the touched/buried ones.
        nd = report.narrative_distortion or {}
        if "new_canonical" in nd:
            n_new = nd["new_canonical"]
            touched = nd["touched_count"]
            buried = nd["suppressed_count"]
        else:                       # fallback for older reports / tests
            canon_new = report.hidden_audit.get("canonical_events", [])
            n_new = len(canon_new) if canon_new else len(report.events)
            touched = len(nd.get("fields", []))
            buried = nd.get("suppressed_count", 0)
        suppressed_total += buried
        # item-weighted share of NEW political chronicle entries the court
        # chronicler softened or buried this turn (§22.5/§22.10). Turns with
        # no political news contribute nothing to either side of the ratio.
        distorted_items += min(n_new, touched + buried)
        political_items += n_new
        civ_a = srv.engine.civilizations["cA"]
        risk_trace.append((round(civ_a.stability_years, 2),
                           round(civ_a.risk_premium, 6)))

    return {"turns": turns, "game_years": round(year, 1), "n_civs": 2,
            "overthrows": overthrows, "conspiracies": conspiracies,
            "distorted_items": distorted_items,
            "political_items": political_items,
            "suppressed": suppressed_total, "risk_trace": risk_trace}


def measure_risk_halflife(trace: list[tuple[float, float]],
                          risk_base: float) -> float | None:
    """Empirical stability-years-to-half from the trace (monotone in tail)."""
    if not trace or risk_base <= 0.0:
        return None
    for s, p in trace:
        if p <= risk_base * 0.5 + 1e-12:
            return s
    return None  # never halved within the run


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--turns", type=int, default=400)
    ap.add_argument("--worlds", type=int, default=8)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--flattery", type=float, default=None)
    ap.add_argument("--censorship", type=float, default=None)
    ap.add_argument("--tau-law", dest="tau_law", type=float, default=None)
    ap.add_argument("--ruler-style", dest="ruler_style", type=float,
                    default=0.6,
                    help="seeded tyranny of both incumbents; distortion "
                         "metrics are only meaningful with style > 0 "
                         "(§22.5/§22.10); pass 0 to keep Rev-3 neutral rulers")
    ap.add_argument("--json", type=Path, default=None,
                    help="write full metrics to this file")
    args = ap.parse_args()

    cfg = build_config(args)
    results = [run_world(make_server(w, cfg, args.ruler_style or None),
                         args.turns) for w in range(args.worlds)]

    total_civ_years = sum(r["game_years"] * r["n_civs"] for r in results)
    overthrows = sum(r["overthrows"] for r in results)
    rate_100 = overthrows / total_civ_years * 100.0 if total_civ_years else 0.0

    distorted = sum(r["distorted_items"] for r in results)
    political = sum(r["political_items"] for r in results)
    depth = distorted / political if political else 0.0

    halflife_emp: float | None = None
    for r in results:
        h = measure_risk_halflife(r["risk_trace"], cfg.equilibrium.risk_base)
        if h is not None:
            halflife_emp = min(halflife_emp, h) if halflife_emp else h
    halflife_theory = math.log(2.0) * cfg.equilibrium.tau_law

    print(f"== Rev 4 calibration ({args.worlds} worlds × {args.turns} turns,"
          f" {total_civ_years:,.0f} civ-years) ==")
    print(f"conspiracy.threshold = {cfg.conspiracy.threshold}")
    print(f"  overthrows         : {overthrows}"
          f"  ({rate_100:.2f} per civ per 100 years)")
    print(f"report flattery/cens.: {cfg.report.flattery_scale}/"
          f"{cfg.report.censorship_level}")
    print(f"  distortion depth   : {depth:.3f}"
          f"  ({distorted}/{political} items),"
          f" suppressed={sum(r['suppressed'] for r in results)}")
    print(f"equilibrium.tau_law  = {cfg.equilibrium.tau_law}")
    print(f"  risk half-life     : theory {halflife_theory:.1f} y,"
          f" empirical {halflife_emp if halflife_emp is not None else '> run'}")

    def warn(name: str, value: float, band: tuple[float, float]) -> None:
        lo, hi = band
        if not (lo <= value <= hi):
            print(f"  WARNING: {name}={value:g} outside target band "
                  f"[{lo}, {hi}] — adjust config/politics.toml (§22.10)")

    warn("conspiracy rate/100y", rate_100, BAND_CONSPIRACY_PER_100Y)
    warn("distortion depth", depth, BAND_DISTORTION)
    warn("risk half-life (theory)", halflife_theory, BAND_RISK_HALFLIFE_YEARS)

    if args.json:
        args.json.write_text(json.dumps(
            {"config": cfg.to_json(),
             "summary": {"civ_years": total_civ_years,
                         "overthrows_per_100y": round(rate_100, 4),
                         "distortion_depth": round(depth, 4),
                         "risk_halflife_theory_y": round(halflife_theory, 2),
                         "risk_halflife_empirical_y": halflife_emp},
             "worlds": results}, ensure_ascii=False, indent=2))
        print(f"metrics written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
