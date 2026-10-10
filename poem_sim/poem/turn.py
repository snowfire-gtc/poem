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
    production_this_turn,
    research_progress_this_turn,
    tradition_gain,
)
from .core import (
    CustomaryLaw,
    Era,
    FolkloreItem,
    Law,
    LawRevision,
    LawRevisionStatus,
    LawStatus,
    LawUserAction,
    SimDuration,
    TimedEffect,
    TimedEffectKind,
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
    economy: dict[str, dict[str, float]] = field(default_factory=dict)
    folklore: dict[str, dict[str, float]] = field(default_factory=dict)
    admin_costs: dict[str, float] = field(default_factory=dict)   # §5, per civ
    notices: list[str] = field(default_factory=list)


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
        self.turns: list[Turn] = []
        self.current_turn: Turn | None = None
        # ids of timed effects that existed before the current turn started
        # applying laws — used to avoid ticking same-turn-created effects
        self._pre_turn_effect_ids: set[str] = set()
        # per-turn bookkeeping
        self._snapshot_list: list[TurnLawSnapshot] = []
        self._deferred_list: list[str] = []
        self._superseded_list: list[str] = []

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

            for law in laws:
                winner = winners.get(law.id)
                if winner is not None:
                    winner.cost = cost_of_law_change(
                        law, winner.action, cfg=self.pricing_cfg,
                        scope_delta=len(winner.raw_text) / 500.0,
                    )
                    self.store.mark_included(winner.id, turn.id)
                    if winner.action in (LawUserAction.REPEAL_LAW,):
                        snap.pending_repeals.append(winner)
                    elif winner.action == LawUserAction.SUSPEND_LAW:
                        snap.suspended_laws.append(law.id)
                        snap.pending_amendments.append(winner)
                    else:
                        snap.pending_amendments.append(winner)
                # A law whose winning revision suspends or repeals it in this
                # turn is no longer part of the *active* set (§4.3), even
                # though its stored status updates only at application time.
                winner_action = winner.action if winner is not None else None
                if winner_action == LawUserAction.SUSPEND_LAW:
                    pass  # already recorded in snap.suspended_laws above
                elif winner_action == LawUserAction.REPEAL_LAW:
                    pass  # recorded in snap.pending_repeals; not active anymore
                elif law.status == LawStatus.ACTIVE:
                    snap.active_laws.append(law)
                elif law.status == LawStatus.SUSPENDED:
                    snap.suspended_laws.append(law.id)
                elif law.status == LawStatus.REPEALED:
                    snap.customary_remnants.append(
                        CustomaryLaw(id=law.id, civilization_id=civ_id,
                                     title=law.title, strength=min(law.tradition, 1.0))
                    )
            snapshots.append(snap)

        turn.status = TurnStatus.SNAPSHOT_PREPARED
        self._snapshot_list = snapshots
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
        for snap in self._snapshot_list:
            self._apply_snapshot_changes(snap, applied)

        # Steps 8–10: time-scaled systems (§11.1–11.3).
        economy: dict[str, dict[str, float]] = {}
        for civ in self.civilizations.values():
            prod = production_this_turn(civ.annual_production_rate, span)
            growth = population_growth_this_turn(civ.population, civ.annual_growth_rate, span)
            civ.population += growth
            research = research_progress_this_turn(
                civ.annual_research_rate, span, civ.culture_modifier, civ.technology_modifier
            )
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
        survivors: list[TimedEffect] = []
        for eff in self.timed_effects:
            if eff.id in pre_turn_effects and eff.tick(span):
                expired.append(eff.id)
                self._on_effect_expired(eff)
            else:
                survivors.append(eff)
        self.timed_effects = survivors

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

        # Mark included revisions as Applied.
        for rev_id in applied:
            self.store.mark_applied(rev_id)

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
                law = self.store.get_law(rev.law_id)
                c = cost_of_law_change(law, rev.action, cfg=self.pricing_cfg)
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
        if era_after != era_before:
            next_span = calculate_turn_span(self.chronology, list(self.civilizations.values()))
            notices.append(
                f"Внимание: благодаря развитию цивилизаций следующий ход охватит "
                f"{next_span.years:g} лет вместо {span.years:g}."
            )

        return TurnReport(
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
            economy=economy,
            folklore=folklore,
            admin_costs=admin_costs,
            notices=notices,
        )

    # ------------------------------------------------------------- internals

    def _apply_snapshot_changes(self, snap: TurnLawSnapshot, applied: list[str]) -> None:
        for rev in snap.pending_amendments + snap.pending_repeals:
            law = self.store.get_law(rev.law_id)
            match rev.action:
                case LawUserAction.REPEAL_LAW:
                    law.status = LawStatus.REPEALED
                case LawUserAction.SUSPEND_LAW:
                    law.status = LawStatus.SUSPENDED
                    years = rev.canonical_change.suspend_years or max(
                        self.chronology.seasons_per_year and 1.0, 1.0
                    )
                    self.timed_effects.append(TimedEffect(
                        id=str(uuid.uuid4()), world_id=self.world_id,
                        civilization_id=law.civilization_id,
                        kind=TimedEffectKind.SUSPENSION,
                        remaining=SimDuration(years=years),
                        payload={"law_id": law.id},
                    ))
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
