"""Simulation cutoff, turn snapshot and the turn pipeline.

TASK-062 (§4, §12, §13).

Core guarantees:
 * The simulation deterministically reads a ``TurnLawSnapshot`` prepared at
   the logical cutoff — nothing else.
 * Revisions confirmed BEFORE the cutoff join this turn; revisions made
   AFTER it are preserved (never lost) and applied in the NEXT turn, with an
   explicit user-facing notice that there is no time-of-day penalty (§4.4).
 * When several edits to one law exist before the cutoff, only the LAST
   confirmed version wins; earlier ones are kept in history as Superseded
   (§4.2).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .chronology import (
    Civilization,
    WorldChronology,
    advance_chronology,
    calculate_turn_span,
    folklore_mutation_gain,
    folklore_spread_gain,
    population_growth_this_turn,
    prejudice_update,
    production_this_turn,
    research_progress_this_turn,
    tradition_gain,
)
from .core import (
    CustomaryLaw,
    Era,
    FolkloreItem,
    Law,
    LawConflict,
    Prejudice,
    LawRevision,
    LawRevisionStatus,
    LawStatus,
    LawUserAction,
    SimDuration,
    TimedEffect,
    TimedEffectKind,
    Treaty,
    War,
    Turn,
    TurnLawSnapshot,
    TurnStatus,
    utcnow,
)
from .laws import LawStore
from .pricing import PricingConfig, cost_of_law_change

# Message shown to the player when an edit misses the cutoff (§4.4, §16.3).
DEFERRED_NOTICE = (
    "Правка сохранена.\n"
    "Она будет учтена при симуляции следующего хода.\n"
    "Причина: правка сделана после среза текущего хода.\n"
    "Штрафа за время суток нет."
)

ACCEPTED_NOTICE = (
    "Правка сохранена на сервере.\n"
    "Она будет учтена при симуляции хода.\n"
    "Время подачи не влияет на эффективность."
)


@dataclass
class TurnReport:
    turn_id: str
    turn_number: int
    era_before: Era
    era_after: Era
    world_year_before: float
    world_year_after: float
    turn_span: SimDuration
    years_elapsed: float
    snapshots: list[TurnLawSnapshot] = field(default_factory=list)
    applied_revisions: list[str] = field(default_factory=list)
    deferred_revisions: list[str] = field(default_factory=list)
    superseded_revisions: list[str] = field(default_factory=list)
    expired_effects: list[str] = field(default_factory=list)
    failed_revisions: dict[str, str] = field(default_factory=dict)  # §3.4 Failed
    economy: dict[str, dict[str, float]] = field(default_factory=dict)
    folklore: dict[str, dict[str, float]] = field(default_factory=dict)
    prejudices: dict[str, dict[str, float]] = field(default_factory=dict)  # §13 step 15
    diplomacy: list[dict] = field(default_factory=list)           # §13 step 16
    wars: dict[str, dict[str, float]] = field(default_factory=dict)  # §13 step 17
    admin_costs: dict[str, float] = field(default_factory=dict)   # §5, per civ
    notices: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)              # §19.1 chronicle
    expired_effect_details: list[dict] = field(default_factory=list)  # for events
    view: dict = field(default_factory=dict)                      # §13 step 19
    image_tasks: list[dict] = field(default_factory=list)         # §13 step 20


class TurnEngine:
    """Owns the current turn and runs the §13 pipeline."""

    def __init__(
        self,
        world_id: str,
        store: LawStore,
        chronology: WorldChronology,
        civilizations: list[Civilization],
        pricing_cfg: PricingConfig | None = None,
        timed_effects: list[TimedEffect] | None = None,
    ) -> None:
        self.world_id = world_id
        self.store = store
        self.chronology = chronology
        self.civilizations = {c.id: c for c in civilizations}
        self.pricing_cfg = pricing_cfg or PricingConfig()
        self.timed_effects: list[TimedEffect] = timed_effects or []
        # Folklore corpus (§11.5): spread/mutation scale with GAME time only.
        self.folklore: list[FolkloreItem] = []
        # Prejudices (§13 step 15): long-lived social biases scaled by game time.
        self.prejudices: list[Prejudice] = []
        # Wars (§13 step 17): conflicts measured in GAME time; fatigue scales
        # with the turn span, never with wall-clock time of day (§20.4).
        self.wars: list[War] = []
        # per-turn prejudice deltas (§13 step 15), keyed by prejudice id
        self._prejudice_delta: dict[str, float] = {}
        self.turns: list[Turn] = []
        self.current_turn: Turn | None = None
        # ids of timed effects that existed before the current turn started
        # applying laws — used to avoid ticking same-turn-created effects
        self._pre_turn_effect_ids: set[str] = set()
        # per-turn bookkeeping
        self._snapshot_list: list[TurnLawSnapshot] = []
        self._deferred_list: list[str] = []
        self._superseded_list: list[str] = []
        # §15.3: last prepared snapshot, readable by the API even after the
        # turn has been finalized (until the next prepare_snapshot overwrites).
        self.last_snapshots: list[TurnLawSnapshot] = []

    # ------------------------------------------------------------------ turns

    def open_turn(self, started_at: datetime, simulation_cutoff_at: datetime, seed: int = 0) -> Turn:
        """Pipeline steps 1–2: open turn, pre-compute planned interval (§13).

        NOTE: ``simulation_cutoff_at`` is part of the *technical schedule*
        (§6.1). It orders edits deterministically but grants no bonuses or
        penalties based on wall-clock time of day (§2).
        """
        civs = list(self.civilizations.values())
        planned_span = calculate_turn_span(self.chronology, civs)
        turn = Turn(
            id=str(uuid.uuid4()),
            world_id=self.world_id,
            number=self.chronology.current_turn + 1,
            started_at=started_at,
            simulation_cutoff_at=simulation_cutoff_at,
            status=TurnStatus.COLLECTING_DIRECTIVES,
            seed=seed,
            turn_span=planned_span,
            world_year_before=self.chronology.current_year,
            era_before=self.chronology.current_era,
        )
        self.current_turn = turn
        self.turns.append(turn)
        return turn

    # ------------------------------------------------------- snapshot (§4.1)

    def prepare_snapshot(self, now: datetime | None = None) -> list[TurnLawSnapshot]:
        """Steps 5–6: reach cutoff, build the final law set per civilization.

        Rules implemented here:
         * A revision participates iff its ``received_at <= cutoff``.
           Only the *confirmation moment relative to the cutoff* matters;
           hour-of-day never does.
         * Per law, among accepted pending revisions only the highest
           ``sequence_number`` is effective; earlier ones become Superseded
           but remain in history (§4.2).
         * Late revisions stay Pending, are marked deferred, and will be
           picked up by the next turn's snapshot — they are NOT discarded
           (§4.4, §20 rule 6).
        """
        turn = self._require_turn()
        cutoff = turn.simulation_cutoff_at
        if now is not None and now < cutoff:
            # Snapshot may only be taken once the cutoff has been reached.
            raise RuntimeError("snapshot requested before simulation_cutoff")

        # Fresh bookkeeping for this turn.
        self._snapshot_list = []
        self._deferred_list = []
        self._superseded_list = []

        snapshots: list[TurnLawSnapshot] = []
        civ_ids = sorted({l.civilization_id for l in self.store._laws.values()})

        # Partition pending revisions against the cutoff and determine the
        # per-law winner BEFORE touching any law state: a suspension that is
        # applied later in this very turn must not remove the law's own
        # winning revision from the snapshot (§4.2 — the last confirmed
        # version wins even if it changes the law's status).
        winners: dict[str, LawRevision] = {}
        deferred_ids: set[str] = set()
        all_pending = self.store.pending_revisions()
        for civ_id in civ_ids:
            by_law: dict[str, list[LawRevision]] = {}
            for rev in (r for r in all_pending if r.civilization_id == civ_id):
                if rev.received_at <= cutoff:
                    by_law.setdefault(rev.law_id, []).append(rev)
                else:
                    # After the cutoff: preserved, applied next turn, no penalty (§4.4).
                    rev.deferred_to_next_turn = True
                    deferred_ids.add(rev.id)
            for law_id, revs in by_law.items():
                revs.sort(key=lambda r: r.sequence_number)
                winners[law_id] = revs[-1]     # last confirmed version wins (§4.2)
                for older in revs[:-1]:        # kept in history as Superseded
                    self.store.mark_superseded(older.id)
                    self._superseded_list.append(older.id)
        self._deferred_list = [
            r.id for r in all_pending if r.id in deferred_ids
        ]

        for civ_id in civ_ids:
            snap = TurnLawSnapshot(turn_id=turn.id, civilization_id=civ_id)
            laws = self.store.laws_of(civ_id)

            # Pass 1: classify every law into the snapshot buckets first, so
            # that conflict detection (§13 step 13) sees the COMPLETE final
            # active set — including laws whose own revisions are processed
            # later in the iteration order (§4.3).
            for law in laws:
                winner = winners.get(law.id)
                winner_action = winner.action if winner is not None else None
                if winner_action == LawUserAction.REPEAL_LAW:
                    pass  # recorded in snap.pending_repeals; not active anymore
                elif winner_action == LawUserAction.SUSPEND_LAW:
                    # §4.2/§4.3: the winning suspension means the law is NOT
                    # part of this turn's active set (it joins suspended_laws
                    # in pass 2, together with its priced revision).
                    pass
                elif law.status == LawStatus.ACTIVE:
                    snap.active_laws.append(law)
                elif law.status == LawStatus.SUSPENDED:
                    snap.suspended_laws.append(law.id)
                elif law.status == LawStatus.REPEALED:
                    snap.customary_remnants.append(
                        CustomaryLaw(id=law.id, civilization_id=civ_id,
                                     title=law.title, strength=min(law.tradition, 1.0))
                    )

            # Pass 2: attach each law's winning revision to the snapshot.
            # Repeals are classified first (their laws leave the active set),
            # then amendments are priced against the POST-repeal legal system
            # (§13 step 13 must see the final norm configuration, §4.3).
            deferred_repeal_pricing: list[tuple] = []
            for law in laws:
                winner = winners.get(law.id)
                if winner is None:
                    continue
                self.store.mark_included(winner.id, turn.id)
                if winner.action == LawUserAction.REPEAL_LAW:
                    snap.pending_repeals.append(winner)
                    deferred_repeal_pricing.append((law, winner))
                    continue
                if winner.action == LawUserAction.SUSPEND_LAW:
                    if law.id not in snap.suspended_laws:
                        snap.suspended_laws.append(law.id)
                snap.pending_amendments.append(winner)

            # Pass 3: price every included revision now that the snapshot's
            # final active set is known.
            # §13 step 13 (norm-conflict detection) feeds the §5.1
            # contradiction_penalty term of the price shown to the player
            # *before* submission (§16: "показывать стоимость"). The
            # prospective canonical form of the amendment is what conflicts
            # with the rest of the legal system — if the revision carries a
            # new canonical, it replaces the old one for conflict purposes.
            repealed_ids = {rev.law_id for _, rev in deferred_repeal_pricing}
            for law in laws:
                winner = winners.get(law.id)
                if winner is None:
                    continue
                effective_canonical = (
                    winner.canonical_change.new_canonical
                    or law.canonical
                )
                contradictions = self._count_conflicts(
                    law, snap, civ_id, prospective=effective_canonical,
                    revision_id=winner.id, exclude_active=repealed_ids)
                winner.cost = cost_of_law_change(
                    law, winner.action, cfg=self.pricing_cfg,
                    scope_delta=len(winner.raw_text) / 500.0,
                    contradictions=contradictions,
                    transition_years=(
                        winner.canonical_change.transition.years
                        if winner.canonical_change.transition else 0.0),
                    urgent=winner.canonical_change.urgent,
                    # §5 «уровень бюрократии»: the civ's own active norm set
                    # (final config of this snapshot) strains administration.
                    active_laws=snap.active_laws,
                )
            snapshots.append(snap)

        turn.status = TurnStatus.SNAPSHOT_PREPARED
        self._snapshot_list = snapshots
        # §15.3: the snapshot itself is a server-side artefact players can
        # inspect (active set, pending edits, suspensions, conflicts).
        self.last_snapshots = snapshots
        return snapshots

    # ------------------------------------------------------------ simulation

    def run_turn(self) -> TurnReport:
        """Pipeline steps 7–22 (economy/legal/diplomacy cores; report)."""
        turn = self._require_turn()
        if turn.status not in (TurnStatus.SNAPSHOT_PREPARED, TurnStatus.COLLECTING_DIRECTIVES):
            raise RuntimeError(f"unexpected turn status {turn.status}")
        if turn.status == TurnStatus.COLLECTING_DIRECTIVES:
            self.prepare_snapshot()
        turn.status = TurnStatus.SIMULATING

        span = calculate_turn_span(self.chronology, list(self.civilizations.values()))
        turn.turn_span = span

        # Steps 7+12: apply confirmed revisions from the snapshot.
        # Snapshot of pre-existing timed effect ids BEFORE application, so
        # effects created by this turn's laws are not ticked/expired now.
        self._pre_turn_effect_ids = {e.id for e in self.timed_effects}
        applied: list[str] = []
        failed: dict[str, str] = {}   # revision_id -> error (§3.4 Failed)
        for snap in self._snapshot_list:
            self._apply_snapshot_changes(snap, applied, failed)

        # Steps 8–10: time-scaled systems (§11.1–11.3).
        economy: dict[str, dict[str, float]] = {}
        for civ in self.civilizations.values():
            prod = production_this_turn(civ.annual_production_rate, span)
            growth = population_growth_this_turn(civ.population, civ.annual_growth_rate, span)
            civ.population += growth
            research = research_progress_this_turn(
                civ.annual_research_rate, span, civ.culture_modifier, civ.technology_modifier
            )
            # §11.3: accumulated research advances the civilization's
            # development index (0..1), which drives era determination (§9)
            # and therefore the Civilization-like turn interval (§6.2).
            if civ.annual_research_rate > 0.0:
                civ.progress = min(1.0, civ.progress + research * civ.research_to_progress)
            economy[civ.id] = {
                "production": prod,
                "population_growth": growth,
                "research": research,
                "population_after": civ.population,
            }
            # Step: update laws' age & tradition proportionally to game time (§11.4).
            for law in self.store.laws_of(civ.id):
                if law.status in (LawStatus.ACTIVE, LawStatus.IN_TRANSITION):
                    law.age_years += span.years
                    law.tradition += tradition_gain(0.002, span)

        # Step 16/§11.6–11.8: tick all timed effects in GAME time.
        # Treaties, law transitions and travel (§11.6, §11.7, §11.8) all
        # consume the turn span from their `remaining` budget; they expire by
        # game time, never by a fixed number of turns.
        # Only effects that existed BEFORE this turn's law application are
        # ticked and expired; a suspension/transition created during this
        # very turn must not complete (and auto-reinstate) within the same
        # turn (§11.7 — transitions finish on later turns' ticks).
        pre_turn_effects = self._pre_turn_effect_ids
        expired: list[str] = []
        expired_details: list[dict] = []
        survivors: list[TimedEffect] = []
        for eff in self.timed_effects:
            if eff.id in pre_turn_effects and eff.tick(span):
                expired.append(eff.id)
                # §19.1: keep enough detail to render the expiry as a
                # chronicle entry (kind + human-readable subject).
                expired_details.append({
                    "effect_id": eff.id,
                    "kind": eff.kind.value,
                    "detail": str(eff.payload.get("law_id")
                                  or eff.payload.get("treaty_kind")
                                  or eff.payload.get("destination", "")),
                })
                self._on_effect_expired(eff)
            else:
                survivors.append(eff)
        self.timed_effects = survivors

        # Step 16 (§13): diplomacy — report the surviving treaties' remaining
        # duration in GAME time (§11.6). Durations are stored in years, never
        # in turn counts; a 100-year treaty ticks down by the turn span each
        # turn and expires by game time (3 turns at 40 y/turn, 100 at 1 y/turn).
        diplomacy: list[dict] = []
        for eff in self.timed_effects:
            if isinstance(eff, Treaty) or eff.kind == TimedEffectKind.TREATY:
                diplomacy.append({
                    "treaty_id": eff.id,
                    "kind": eff.payload.get("treaty_kind", "peace"),
                    "civilization_id": eff.civilization_id,
                    "remaining_years": max(eff.remaining.years, 0.0),
                })
            elif eff.kind == TimedEffectKind.TRAVEL:
                diplomacy.append({
                    "travel_id": eff.id,
                    "kind": "envoy_travel",
                    "civilization_id": eff.civilization_id,
                    "remaining_years": max(eff.remaining.years, 0.0),
                })

        # Step 17 (§13): wars — fatigue accumulates proportionally to the
        # turn's GAME-time span with diminishing gains (§19.1: not every
        # process scales linearly). An exhausted side sues for peace: the war
        # ends and a peace treaty is concluded for 25 game-years — its clock
        # then runs in game time like every other duration (§11.6).
        wars: dict[str, dict[str, float]] = {}
        for war in self.wars:
            if not war.active:
                continue
            war.tick(span.years)
            if war.exhausted:
                war.active = False
                self.timed_effects.append(Treaty(
                    id=str(uuid.uuid4()), world_id=self.world_id,
                    civilization_id=war.attacker_id,
                    kind=TimedEffectKind.TREATY,
                    remaining=SimDuration(years=25.0),
                    payload={"treaty_kind": "peace",
                             "ended_war_id": war.id,
                             "other_party": war.defender_id},
                ))
            wars[war.id] = {
                "active": 1.0 if war.active else 0.0,
                "fatigue_attacker": round(war.fatigue_attacker, 4),
                "fatigue_defender": round(war.fatigue_defender, 4),
            }

        # Step 14 (§11.5): update folklore — spread & mutation scale with the
        # turn's GAME-time span, never with real-world time of day (§20.4).
        # Laws repealed in this very turn also leave customary remnants that
        # feed their topic's mutation (conflict with living memory of the law).
        remnant_topics = {
            r.id for snap in self._snapshot_list for r in snap.customary_remnants
        }
        for rev_id in applied:
            rev = self.store.get_revision(rev_id)
            if rev.action == LawUserAction.REPEAL_LAW:
                remnant_topics.add(rev.law_id)
        # A FAILED repeal (§3.4) did not actually remove the law, so it must
        # not linger in folk memory as a customary remnant.
        for rev_id in failed:
            rev = self.store.get_revision(rev_id)
            if rev.action == LawUserAction.REPEAL_LAW:
                remnant_topics.discard(rev.law_id)
        folklore: dict[str, dict[str, float]] = {}
        for item in self.folklore:
            if item.topic in remnant_topics and item.conflict_factor <= 0.0:
                item.conflict_factor = 0.5  # repealed law lingers in folk memory
            s_gain = folklore_spread_gain(0.01, span, item.oral_strength)
            m_gain = folklore_mutation_gain(
                0.005, span, item.oral_strength, item.distance_factor,
                # 0 conflict => neutral multiplier 1.0; any tension amplifies
                # mutation (§11.5 conflict_factor).
                1.0 + item.conflict_factor,
            )
            item.spread += s_gain
            item.mutation += m_gain
            folklore[item.id] = {
                "spread": item.spread,
                "mutation": item.mutation,
                "spread_gain": s_gain,
                "mutation_gain": m_gain,
            }

        # Step 15 (§13): update prejudices — long-lived social biases that
        # intensify while their underlying conflict is alive and fade once it
        # is resolved. All changes scale with the turn's GAME-time span
        # (§20.9); a 40-year ancient turn moves them far more than a 1-year
        # modern turn. Wall-clock time of day never enters this step (§20.4).
        # Repealing a contested law marks its prejudice as resolving: folk
        # hostility toward the targeted group decays from then on.
        for p in self.prejudices:
            if not p.resolved and p.target in remnant_topics:
                p.resolved = True
            delta = prejudice_update(p, span)
            self._prejudice_delta[p.id] = delta
        prejudices: dict[str, dict[str, float]] = {
            p.id: {"intensity": p.intensity, "delta": self._prejudice_delta[p.id],
                   "resolved": 1.0 if p.resolved else 0.0}
            for p in self.prejudices
        }

        # Mark included revisions as Applied (successful applications only).
        for rev_id in applied:
            self.store.mark_applied(rev_id)
        # §3.4 Failed — a revision that joined the slice but whose
        # application raised is marked Failed and stays in history; it is
        # never charged (§5) and never silently retried.
        for rev_id, err in failed.items():
            self.store.mark_failed(rev_id, reason=err)

        # §5, §11.4 — law-change cost is paid from the administrative budget
        # that this turn's production accumulates (in GAME time). The cost
        # formula itself never sees wall-clock time (§20.5): a change made at
        # 03:00 costs exactly what the same change costs at 18:00.
        admin_costs: dict[str, float] = {}
        for snap in self._snapshot_list:
            civ = self.civilizations.get(snap.civilization_id)
            if civ is None:
                continue
            budget = economy[snap.civilization_id]["production"] + civ.administrative_budget
            for rev in snap.pending_amendments + snap.pending_repeals:
                if rev.id in failed:
                    # §3.4: a revision that FAILED to apply is not charged —
                    # the player pays only for changes that actually happen.
                    continue
                # Charge the EXACT price computed at snapshot time (§5.1):
                # same inputs (law state, scope_delta, detected norm
                # conflicts) as the preview shown to the player before
                # submission — no drift between preview and charge.
                c = rev.cost if rev.cost is not None else cost_of_law_change(
                    self.store.get_law(rev.law_id), rev.action, cfg=self.pricing_cfg)
                budget -= c
                admin_costs.setdefault(snap.civilization_id, 0.0)
                admin_costs[snap.civilization_id] += c
            civ.administrative_budget = max(budget, 0.0)

        # Step 18: advance world chronology.
        era_before, era_after = advance_chronology(self.chronology, span, list(self.civilizations.values()))
        turn.status = TurnStatus.FINALIZING
        turn.turn_span = span
        turn.world_year_after = self.chronology.current_year
        turn.era_before, turn.era_after = era_before, era_after
        turn.finalized_at = utcnow()
        turn.status = TurnStatus.FINALIZED
        # Deferred revisions stay Pending (not lost) and join the next turn's
        # snapshot; clear their deferred flag so the next cutoff re-evaluates
        # them cleanly (§4.4).
        for rev_id in self._deferred_list:
            self.store.get_revision(rev_id).deferred_to_next_turn = False
        self.current_turn = None

        notices: list[str] = []
        if self._deferred_list:
            notices.append(DEFERRED_NOTICE)
        if failed:
            # §3.4/§16.3 — the player is told explicitly which edit failed to
            # apply; it is preserved in history, never charged, and does NOT
            # silently retry next turn.
            ids = ", ".join(failed.keys())
            notices.append(
                f"Правка не применена: симуляция хода завершила её со статусом Failed.\n"
                f"Ревизии: {ids}.\nОна сохранена в истории версий; стоимость не списана."
            )
        if era_after != era_before:
            next_span = calculate_turn_span(self.chronology, list(self.civilizations.values()))
            # §16.1 — the warning is only meaningful when the interval actually
            # changed; an era shift with identical span must not emit a noisy
            # (and non-deterministic-looking) notice.
            if next_span.years != span.years:
                notices.append(
                    f"Внимание: благодаря развитию цивилизаций следующий ход охватит "
                    f"{next_span.years:g} лет вместо {span.years:g}."
                )

        report = TurnReport(
            turn_id=turn.id,
            turn_number=turn.number,
            era_before=era_before,
            era_after=era_after,
            world_year_before=turn.world_year_before,
            world_year_after=turn.world_year_after,
            turn_span=span,
            years_elapsed=span.years,
            snapshots=self._snapshot_list,
            applied_revisions=applied,
            deferred_revisions=list(self._deferred_list),
            superseded_revisions=list(self._superseded_list),
            expired_effects=expired,
            failed_revisions=dict(failed),
            economy=economy,
            folklore=folklore,
            prejudices=prejudices,
            diplomacy=diplomacy,
            wars=wars,
            admin_costs=admin_costs,
            notices=notices,
            expired_effect_details=expired_details,
        )

        # §19.1: build the turn chronicle ("events for the interval") BEFORE
        # the view, so clients receive intermediate cultural milestones and
        # key events instead of a bare time jump.
        from .views import (
            build_turn_view, interval_events, queue_image_tasks)

        report.events = [e.to_json() for e in
                         interval_events(report, turn.world_year_before)]

        # Steps 19–20 (§13): generate client representations from the finalized
        # report, then queue chronicle illustration tasks derived from them.
        report.view = build_turn_view(
            report, self.chronology, list(self.civilizations.values()))
        report.image_tasks = [t.to_json() for t in queue_image_tasks(report.view, self.world_id)]

        return report

    # ------------------------------------------------------------- internals

    def _count_conflicts(
        self, law, snap: TurnLawSnapshot, civ_id: str, prospective=None,
        revision_id: str | None = None, exclude_active: set[str] | None = None,
    ) -> int:
        """§13 step 13 (norm-conflict detection), used for §5 pricing.

        A revision whose (possibly new) canonical form contradicts another
        active law of the same civilization or a surviving customary remnant
        pays ``contradiction_penalty`` per conflict (§5.1). Deterministic and
        independent of wall-clock time. Detected conflicts are also recorded
        on the snapshot (§4.3 ``conflicts``) so clients can display them
        (§16.2). Each entry names the pending edit that creates the clash
        (``revision_id``) so the UI can point at the exact amendment.

        Three kinds of contradiction are recognised:
         * identical canonical kind — two laws regulating the same matter;
         * conflicting kind pair (``kinds_conflict``, e.g. tax vs ban);
         * an incoming REPEAL that removes a law another ACTIVE law still
           depends on — repealing the enforcement norm while the obligation
           norm survives is itself a legal-system contradiction (§5.1
           «конфликт с другими законами»).

        ``exclude_active`` lists laws that will be repealed within this very
        snapshot: they must not count as conflict partners any more, because
        step 13 evaluates against the FINAL norm configuration (§4.3).
        """
        from .core import kinds_conflict

        canon = prospective if prospective is not None else law.canonical
        excluded = exclude_active or set()
        count = 0

        # Repeal-dependency conflict: does any remaining active law reference
        # this law (canonical refs or title mention)? Repealing it while the
        # referencing law stays active produces a contradiction.
        if revision_id is not None:
            rev = None
            from .laws import LawStoreError
            try:
                rev = self.store.get_revision(revision_id)
            except (KeyError, LawStoreError):
                rev = None
            if rev is not None and rev.action == LawUserAction.REPEAL_LAW:
                for other in snap.active_laws:
                    if other.id == law.id or other.id in excluded:
                        continue
                    depends = (
                        law.id in getattr(other.canonical, "refs", ())
                        if other.canonical is not None else False
                    ) or (bool(law.title) and law.title in other.raw_text)
                    if depends:
                        count += 1
                        snap.conflicts.append(
                            LawConflict(law_a=law.id, law_b=other.id,
                                        revision_id=revision_id,
                                        severity=0.75,
                                        description=(
                                            f"repealing {law.title!r} leaves "
                                            f"{other.title!r} without its "
                                            "referenced norm"))
                        )

        for other in snap.active_laws:
            if other.id == law.id or other.id in excluded:
                continue
            if canon is not None and other.canonical is not None:
                same_kind = other.canonical.kind == canon.kind
                opposed = kinds_conflict(canon.kind, other.canonical.kind)
                if same_kind or opposed:
                    count += 1
                    snap.conflicts.append(
                        LawConflict(law_a=law.id, law_b=other.id,
                                    revision_id=revision_id,
                                    description=(
                                        "two active laws of the same kind"
                                        if same_kind else
                                        f"kinds {canon.kind!r} and "
                                        f"{other.canonical.kind!r} contradict"))
                    )
        for rem in snap.customary_remnants:
            if rem.title == law.title:
                count += 1
                snap.conflicts.append(
                    LawConflict(law_a=law.id, law_b=None, folklore_ref=rem.title,
                                revision_id=revision_id,
                                description="law contradicts a customary remnant")
                )
        return count

    def _apply_snapshot_changes(
        self, snap: TurnLawSnapshot, applied: list[str],
        failed: dict[str, str] | None = None,
    ) -> None:
        """Steps 7+12 of §13. A revision whose application raises is recorded
        in ``failed`` (§3.4 Failed) and never lands in ``applied`` — the turn
        continues with the remaining laws (partial failure is isolated)."""
        if failed is None:
            failed = {}
        for rev in snap.pending_amendments + snap.pending_repeals:
          try:
            law = self.store.get_law(rev.law_id)
            match rev.action:
                case LawUserAction.REPEAL_LAW:
                    law.status = LawStatus.REPEALED
                case LawUserAction.SUSPEND_LAW:
                    law.status = LawStatus.SUSPENDED
                    years = rev.canonical_change.suspend_years or max(
                        self.chronology.seasons_per_year and 1.0, 1.0
                    )
                    # §4.2 example: "приостановить закон на один сезон" — the
                    # suspension clock runs in GAME time (§11.7/§20.9): the
                    # requested duration is interpreted directly in years.
                    eff = TimedEffect(
                        id=str(uuid.uuid4()), world_id=self.world_id,
                        civilization_id=law.civilization_id,
                        kind=TimedEffectKind.SUSPENSION,
                        remaining=SimDuration(years=years),
                        payload={"law_id": law.id},
                    )
                    self.timed_effects.append(eff)
                    snap.suspended_laws.append(law.id)   # §4.3 snapshot field
                case LawUserAction.REINSTATE_LAW:
                    law.status = LawStatus.ACTIVE
                case LawUserAction.AMEND_LAW | LawUserAction.SUBMIT_DRAFT:
                    if rev.canonical_change.new_canonical is not None:
                        law.canonical = rev.canonical_change.new_canonical
                        law.raw_text = rev.raw_text
                    if rev.canonical_change.transition is not None:
                        law.status = LawStatus.IN_TRANSITION
                        self.timed_effects.append(TimedEffect(
                            id=str(uuid.uuid4()), world_id=self.world_id,
                            civilization_id=law.civilization_id,
                            kind=TimedEffectKind.LAW_TRANSITION,
                            remaining=rev.canonical_change.transition,
                            payload={"law_id": law.id,
                                     "final_status": LawStatus.ACTIVE.value},
                        ))
                case LawUserAction.CLARIFY_LAW:
                    law.raw_text = rev.raw_text
          except Exception as exc:      # §3.4 Failed — isolate this revision
            failed[rev.id] = str(exc)
            continue
          applied.append(rev.id)

    def _on_effect_expired(self, eff: TimedEffect) -> None:
        if eff.kind == TimedEffectKind.LAW_TRANSITION:
            law = self.store.get_law(eff.payload["law_id"])
            if law.status == LawStatus.IN_TRANSITION:
                law.status = LawStatus.ACTIVE       # §11.7 transition finished
        elif eff.kind == TimedEffectKind.SUSPENSION:
            law = self.store.get_law(eff.payload["law_id"])
            if law.status == LawStatus.SUSPENDED:
                law.status = LawStatus.ACTIVE       # auto-reinstate after suspension

    def _require_turn(self) -> Turn:
        if self.current_turn is None:
            raise RuntimeError("no active turn; call open_turn first")
        return self.current_turn
