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
    language: dict[str, dict] = field(default_factory=dict)       # TASK-068, per civ census
    language_events: list[dict] = field(default_factory=list)     # splits/merges this turn
    notices: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)              # §19.1 chronicle
    expired_effect_details: list[dict] = field(default_factory=list)  # for events
    ruler_reports: list[dict] = field(default_factory=list)  # §13 st.22 literary reports
    view: dict = field(default_factory=dict)                      # §13 step 19
    image_tasks: list[dict] = field(default_factory=list)         # §13 step 20
    # --- Rev 4 (§22.6): political layer outputs -----------------------------
    # politics_active=False ⇒ every field below stays empty/default and the
    # report is byte-identical to a Rev-3 one (backward-compatibility rule).
    politics_active: bool = False
    fortune_events: list[dict] = field(default_factory=list)      # public, no rolls
    elites: dict[str, list[dict]] = field(default_factory=dict)   # public bands only
    conspiracies: list[dict] = field(default_factory=list)        # chronicle facts
    rulers: list[dict] = field(default_factory=list)              # public bands
    narrative_distortion: dict = field(default_factory=dict)      # §22.6 step 19a audit
    # --- TASK-074 (§22.4): market layer public projections. Empty while
    # politics is disabled — Rev-3 byte identity holds (§22.9-3).
    markets: list[dict] = field(default_factory=list)             # saturation bands, exporters
    influence: list[dict] = field(default_factory=list)           # dominant/presence band
    # server-side ONLY (admin raw endpoint): hidden rolls & audits per civ
    hidden_audit: dict = field(default_factory=dict)


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
        politics=None,                      # poem.politics.PoliticsConfig | None
        world_seed: int = 0,                # §22.1: server-side only, never shipped
    ) -> None:
        self.world_id = world_id
        self.store = store
        self.chronology = chronology
        self.civilizations = {c.id: c for c in civilizations}
        self.pricing_cfg = pricing_cfg or PricingConfig()
        self.timed_effects: list[TimedEffect] = timed_effects or []
        # Rev 4 (§22): political layer. Default OFF ⇒ every R4 hook below is
        # a no-op and the engine behaves exactly like Rev 3 (hard rule).
        from .politics import PoliticsConfig as _PC, WorldRng as _WR
        self.politics = politics if isinstance(politics, _PC) else _PC()
        self.rng = _WR(world_seed, world_id)
        # faction registry per civilization (created lazily on first turn
        # with politics enabled; empty while disabled)
        self.factions: dict[str, list] = {}
        # conspiracy events history (chronicle facts; rolls live in snapshots)
        self.conspiracies: list = []
        # §22.5 resurfacing queue ("скрытая правда"): chronicle items the
        # court chronicler censored in past turns reappear later — as tax
        # arrears, rumors, belated notices.  Server-side memory; drained
        # into the NEXT turn's narrative (§22.5, TASK-076).
        self.suppressed_chronicle: list[dict] = []
        # per-civ charged law-change costs of the CURRENT turn, recorded in
        # the admin-cost step so step 17a can derive this interval's tax
        # pressure (extraction felt by the elites, §22.3).
        self._charged_costs_by_civ: dict[str, list[float]] = {}
        # Rev 4: hidden Fortune rolls of civilizations that submitted no legal
        # snapshot this turn — they still strike (§22.1), so their raw rolls
        # are kept here for the admin audit / chronicle projection.
        self._orphan_fortune_rolls: list = []
        # Rev 4 TASK-074 (§22.4): market/niche and influence layer. Created
        # lazily on the first turn with politics enabled; while disabled the
        # engine never touches them (Rev-3 identity, §22.9-3).
        self.niche_map: dict = {}
        self.influence_regions: dict = {}
        # last finalized TurnReport (Rev 4 audit consumers: admin endpoint)
        self.last_report: "TurnReport | None" = None
        # Folklore corpus (§11.5): spread/mutation scale with GAME time only.
        self.folklore: list[FolkloreItem] = []
        # Prejudices (§13 step 15): long-lived social biases scaled by game time.
        self.prejudices: list[Prejudice] = []
        # Wars (§13 step 17): conflicts measured in GAME time; fatigue scales
        # with the turn span, never with wall-clock time of day (§20.4).
        self.wars: list[War] = []
        # TASK-068: language & dialect tree (koine drift/split/merge), driven
        # once per turn in GAME time. Settlements register here; each civ's
        # ``language_unity`` feeds law pricing and report stylisation.
        from .language import LanguageState
        self.language = LanguageState()
        for civ in civilizations:
            if civ.id not in self.language.civilizations:
                self.language.add_civilization(civ, chronology.current_era,
                                               chronology.current_year)
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
        # §13 step 20: server-side queue of chronicle illustration tasks.
        # Tasks are created when a turn finalizes and transition
        # queued -> running -> done/failed as renderers claim them; they are
        # kept until acknowledged so clients can poll progress (§16).
        self.image_queue: list = []
        # TASK-067: visual identity memory in server RAM — faces of
        # historical persons, appearances of inventions, paintings, places.
        # queue_image_tasks consults it so recurring subjects keep the same
        # look across chronicle illustrations (deterministic, game-time only).
        from .tokens import TokenRegistry
        self.tokens = TokenRegistry(world_id)

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
                    # TASK-068: decrees cost more where speech is fragmented —
                    # every dialect that barely understands the koine adds
                    # translators, scribes and doubting clerks to enforcement.
                    # A world without settlements speaks one common tongue:
                    # fragmentation stays 0 and prices match the classic
                    # formula exactly (backward compatibility).
                    language_fragmentation=self._language_fragmentation(civ_id),
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
        # §4.4 "picked up by the next turn": an already-applied revision must
        # never be re-applied by a second run_turn() on the same open turn —
        # otherwise its suspension/transition effect would get a duplicate
        # provenance stamp and could complete inside its own birth turn.
        pending_revs = [rev for snap in self._snapshot_list
                        for rev in snap.pending_amendments + snap.pending_repeals
                        if rev.status != LawRevisionStatus.APPLIED]
        for rev in pending_revs:
            rev.status = LawRevisionStatus.INCLUDED_IN_SIMULATION
        # Snapshot of pre-existing timed effect ids BEFORE application, so
        # effects created by this turn's laws are not ticked/expired now.
        self._pre_turn_effect_ids = {e.id for e in self.timed_effects}
        applied: list[str] = []
        failed: dict[str, str] = {}   # revision_id -> error (§3.4 Failed)
        applied_set: set[str] = set()  # provenance set for _born_this_turn (§11.7)
        for snap in self._snapshot_list:
            self._apply_snapshot_changes(
                snap, applied, failed, turn_id_of_application=turn.id)
        for rid in applied:           # §3.4: applied revisions are final
            self.store.get_revision(rid).status = LawRevisionStatus.APPLIED
        applied_set = set(applied)

        # §11.7: timed effects spawned by THIS turn's directives — either by
        # law application just above or by the exhausted-war peace below —
        # must not complete within the very turn that created them; they
        # join the tick pool only on the next turn.  Provenance is keyed by
        # the finalized turn id, so several run_turn() calls inside one open
        # turn cannot confuse the guard (a plain "born this run" snapshot
        # would wrongly exempt pre-existing treaties from ticking).
        def _born_this_turn(e: TimedEffect) -> bool:
            if e.payload.get("created_by_war_exhaustion") == turn.id:
                return True
            rid = e.payload.get("created_by_revision")
            return rid is not None and rid in applied_set

        # Steps 8–10: time-scaled systems (§11.1–11.3).
        # --- Rev 4 step 13a (BEFORE the economy so shocks hit production of
        # THIS interval, §22.6): hidden Fortune rolls per civilization.
        # Rolls run for EVERY active civilization — not only those that
        # submitted a legal snapshot this turn: Fortune strikes fields and
        # plagues cities whether or not the throne decreed anything (§22.1).
        # No-op while politics.enabled=false ⇒ Rev-3 pipeline untouched.
        fortune_impacts: dict[str, dict[str, float]] = {}
        if self.politics.enabled:
            from .fortune import roll_fortune
            rolled_for: set[str] = set()
            for snap in self._snapshot_list:
                civ = self.civilizations.get(snap.civilization_id)
                if civ is None or not civ.active or civ.id in rolled_for:
                    continue
                rolled_for.add(civ.id)
                kinds = {l.canonical.kind for l in snap.active_laws
                        if l.canonical is not None}
                statecraft = civ.ruler.statecraft if civ.ruler else 0.0
                rolls, impacts = roll_fortune(
                    self.rng, turn.number, civ.id, statecraft, kinds,
                    self.politics.fortune)
                snap.fortune_rolls.extend(rolls)          # server-side only
                fortune_impacts[civ.id] = impacts
            # civilizations without a snapshot this turn still draw Fortune
            for cid, civ in self.civilizations.items():
                if not civ.active or cid in rolled_for:
                    continue
                statecraft = civ.ruler.statecraft if civ.ruler else 0.0
                rolls, impacts = roll_fortune(
                    self.rng, turn.number, cid, statecraft, set(),
                    self.politics.fortune)
                self._orphan_fortune_rolls.extend(rolls)  # admin audit only
                fortune_impacts[cid] = impacts

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

        # --- Rev 4 step 8a (§22.3 TASK-073): risk-premium decay driven by
        # stability_years; long calm cheapens capital, cheap capital breeds
        # interlinked investment — complexity grows, resilience falls, so a
        # Fortune shock later costs more. Plus §22.1-5 shock application:
        # impacts of THIS turn's adverse rolls damp production/population.
        # No-op while politics is disabled (Rev-3 identity, §22.9-3).
        if self.politics.enabled:
            from .chronology import risk_premium_value
            eq = self.politics.equilibrium
            for civ in self.civilizations.values():
                if not civ.active:
                    continue
                edits_this_turn = any(
                    snap.civilization_id == civ.id
                    and (snap.pending_amendments or snap.pending_repeals)
                    for snap in self._snapshot_list)
                repeals_this_turn = any(
                    snap.civilization_id == civ.id and snap.pending_repeals
                    for snap in self._snapshot_list)
                if repeals_this_turn:
                    civ.stability_years = 0.0     # repeal resets fully (§22.3)
                elif edits_this_turn:
                    civ.stability_years *= 0.5    # an edit partially resets
                else:
                    civ.stability_years += span.years
                civ.risk_premium = risk_premium_value(
                    civ.stability_years, eq.risk_base, eq.tau_law)
                # cheap capital → investment → interlinking (§22.3)
                invest = (1.0 - civ.risk_premium) * eq.complexity_investment_gain \
                    * span.years
                civ.complexity = max(1.0, civ.complexity + invest * civ.progress)
                civ.resilience = 1.0 / civ.complexity
                # apply Fortune shocks of this interval, scaled by fragility
                impacts = fortune_impacts.get(civ.id, {})
                prod_hit = impacts.get("production", 0.0)
                if prod_hit:
                    hit = prod_hit * prod * (2.0 - civ.resilience)
                    economy[civ.id]["production"] = max(0.0, prod - hit)
                    economy[civ.id]["shock_damage"] = round(hit, 4)
                pop_hit = impacts.get("population", 0.0)
                if pop_hit:
                    loss = pop_hit * civ.population * (2.0 - civ.resilience)
                    civ.population = max(1.0, civ.population - loss)
                    economy[civ.id]["population_after"] = civ.population
                    economy[civ.id]["shock_population_loss"] = round(loss, 4)
                unrest = impacts.get("unrest", 0.0)
                if unrest:
                    civ.militarization = min(1.0, civ.militarization + unrest * 0.1)
                if impacts.get("folklore"):
                    for item in self.folklore:
                        if item.civilization_id == civ.id:
                            item.spread = min(1.5, item.spread + 0.05 * span.years * 0.1)

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
            if (eff.id in pre_turn_effects or not _born_this_turn(eff)) \
                    and eff.tick(span):
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
        # Treaties concluded during THIS very turn (e.g. an exhausted war that
        # sued for peace above) are reported too — they exist from now on,
        # they are simply not ticked until the next turn's span (§11.7).
        diplomacy: list[dict] = []
        for eff in self.timed_effects:
            if isinstance(eff, Treaty) or eff.kind == TimedEffectKind.TREATY:
                entry = {
                    "treaty_id": eff.id,
                    "kind": eff.payload.get("treaty_kind", "peace"),
                    "civilization_id": eff.civilization_id,
                    "remaining_years": max(eff.remaining.years, 0.0),
                }
                # §19.1 / ruler report: a peace that ended a war names the
                # war and the other party so chronicles can tell them apart.
                if eff.payload.get("ended_war_id"):
                    entry["ended_war_id"] = eff.payload["ended_war_id"]
                if eff.payload.get("other_party"):
                    entry["other_party"] = eff.payload["other_party"]
                diplomacy.append(entry)
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
        war_parties: dict[str, dict[str, str | None]] = {
            # §19.1 / ruler report: include finished wars too — a peace
            # concluded THIS turn must still know who fought whom.
            w.id: {"attacker_id": w.attacker_id, "defender_id": w.defender_id}
            for w in self.wars
        }
        for war in self.wars:
            if not war.active:
                continue
            war.tick(span.years)
            if war.exhausted:
                war.active = False
                peace = Treaty(
                    id=str(uuid.uuid4()), world_id=self.world_id,
                    civilization_id=war.attacker_id,
                    kind=TimedEffectKind.TREATY,
                    remaining=SimDuration(years=25.0),
                    payload={"treaty_kind": "peace",
                             "ended_war_id": war.id,
                             "other_party": war.defender_id,
                             # §11.7 provenance: concluded THIS turn, so it
                             # must not be ticked/expired until next turn.
                             "created_by_war_exhaustion": turn.id},
                )
                self.timed_effects.append(peace)
                # The diplomacy section above was built BEFORE wars were
                # resolved; a peace concluded on THIS turn must still reach
                # the ruler report ("Слово о мире"), so append its entry now.
                diplomacy.append({
                    "treaty_id": peace.id,
                    "kind": "peace",
                    "civilization_id": peace.civilization_id,
                    "remaining_years": max(peace.remaining.years, 0.0),
                    "ended_war_id": war.id,
                    "other_party": war.defender_id,
                })
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

        # --- Rev 4 steps 17a/17b (§22.2, §22.3; TASK-071/072/075): elites,
        # conspiracies with mechanical succession, equilibrium revert and
        # ву-вэй accumulation. Runs after step 17 (war outcomes feed elite
        # grievance) and before the chronicle is assembled. Entirely gated
        # by politics.enabled — while it is off nothing below runs and the
        # pipeline stays byte-identical to Rev 3 (§22.9-3).
        # NOTE on ordering: run_turn() applies the snapshot's revisions
        # FIRST (step 7+12), so by this point every charged cost of the
        # interval is already known from the prices computed at snapshot
        # time (§5.1 preview = charge). We record them here for step 17a's
        # tax-pressure proxy.
        politics_out: dict = {}
        markets_out: dict = {}
        if self.politics.enabled:
            # --- Rev 4 step 16a (§22.4, TASK-074): markets & niches run
            # BEFORE the elite step so this turn's market_grievance feeds
            # faction grievance in 17a; all rolls come from WorldRng and
            # stay server-side (hidden AI bids, §22.8).
            markets_out = self._run_markets_step(turn, span, economy)
            charged: dict[str, list[float]] = {}
            for snap in self._snapshot_list:
                for rev in snap.pending_amendments + snap.pending_repeals:
                    if rev.id in failed:
                        continue      # §3.4: a Failed revision is never paid
                    c = rev.cost if rev.cost is not None else cost_of_law_change(
                        self.store.get_law(rev.law_id), rev.action,
                        cfg=self.pricing_cfg)
                    if c > 0.0:
                        charged.setdefault(snap.civilization_id, []).append(c)
            self._charged_costs_by_civ = charged
            # this turn's market losses feed faction grievance in 17a (§22.4-1)
            politics_out = self._run_politics_step(
                turn, span, economy,
                market_grievances=markets_out.get("grievances", {}))

        # TASK-068 (§ language design): advance the koine/dialect tree.
        # Drift, road growth and unification all scale with the turn's
        # GAME-time span — a 40-year ancient turn reshapes speech far more
        # than a 1-year modern one; wall-clock time never enters (§20.4).
        lang_events = self.language.tick(span, era=self.chronology.current_era,
                                         current_year=self.chronology.current_year)
        language_census: dict[str, dict] = {}
        for cid in self.civilizations:
            cen = self.language.census(cid)
            if cen:
                language_census[cid] = cen

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
            language=language_census,
            language_events=lang_events,
            notices=notices,
            expired_effect_details=expired_details,
            # --- Rev 4 (§22.6): political layer outputs. While politics is
            # disabled everything below stays at its empty default and the
            # report is byte-identical to a Rev-3 one (§22.9-3).
            politics_active=self.politics.enabled,
            elites=(politics_out.get("public", {}).get("elites_by_civ", {})
                    if self.politics.enabled else {}),
            conspiracies=(politics_out.get("public", {}).get("conspiracies", [])
                          if self.politics.enabled else []),
            rulers=(politics_out.get("public", {}).get("rulers", [])
                    if self.politics.enabled else []),
            markets=(markets_out.get("public", [])
                     if self.politics.enabled else []),
            influence=([r.to_public_json()
                        for r in self.influence_regions.values()]
                       if self.politics.enabled else []),
            hidden_audit={
                "charged_costs": {cid: sum(v) for cid, v in
                                  self._charged_costs_by_civ.items()},
                **({
                    "market_bids": markets_out.get("hidden", {}).get("bids", {}),
                    "market_grievances": markets_out.get("grievances", {}),
                    "elite_deltas": politics_out.get("hidden", {}).get("elite_deltas", {}),
                    "conspiracy_audit": politics_out.get("hidden", {}).get("conspiracy_audit", {}),
                    "successions": politics_out.get("hidden", {}).get("successions", []),
                    "rng": self.rng.snapshot_state(turn.number),
                } if self.politics.enabled else {}),
            },
        )

        # §19.1: build the turn chronicle ("events for the interval") BEFORE
        # the view, so clients receive intermediate cultural milestones and
        # key events instead of a bare time jump.
        from .views import (
            build_turn_view, interval_events, queue_image_tasks)

        report.events = [e.to_json() for e in
                         interval_events(report, turn.world_year_before)]

        # --- Rev 4 steps 19a/19b (§22.5, TASK-076): Fortune consequences and
        # conspiracy facts join the CHRONICLE as public events (no numbers),
        # then the court chronicler distorts the narrative copy while the
        # canonical list stays honest (§22.5 hard contract).
        if self.politics.enabled:
            from .distortion import distort_chronicle
            canon_start = len(report.events)
            # every active civ's successful adverse rolls join the chronicle,
            # whether they were carried on a legal snapshot or rolled for a
            # civ that decreed nothing this turn (§22.1)
            all_rolls: list[tuple[str, object]] = []
            for snap in self._snapshot_list:
                for roll in snap.fortune_rolls:
                    all_rolls.append((snap.civilization_id, roll))
            for roll in self._orphan_fortune_rolls:
                all_rolls.append((roll.civ_id or "", roll))
            self._orphan_fortune_rolls = []
            for cid, roll in all_rolls:
                if not roll.success:
                    continue
                entry = dict(roll.to_event_json())
                entry["civilization_id"] = cid
                entry["year"] = round(self.chronology.current_year, 2)
                entry["canonical"] = True
                # adverse rolls that struck are recorded honestly here;
                # distortion happens only in the narrative projection
                entry["_severity"] = roll.severity   # stripped below
                report.events.append(entry)
            for ev_json in report.conspiracies:
                ce = dict(ev_json)
                ce["kind"] = "ConspiracyEvent"  # _is_political() hook
                ce["title"] = ("Свержение правителя" if
                               ce.get("outcome") == "ruler_overthrown"
                               else "Заговор раскрыт")
                ce["detail"] = (f"{ce['instigator']}: {ce['outcome']}")
                ce["year"] = ce.get("year", self.chronology.current_year)
                ce["canonical"] = True
                report.events.append(ce)
            # resurfacing suppressed truth (§22.5 «скрытая правда»): censored
            # items from earlier turns reappear now — late, but honest.
            drained = self.suppressed_chronicle[:2]
            self.suppressed_chronicle = self.suppressed_chronicle[2:]
            for old in drained:
                item = dict(old)
                item["title"] = f"[Позднее сообщение] {item.get('title', '')}"
                item["detail"] = (item.get("detail", "") +
                                  " Истина проступает сквозь молчание летописи.").strip()
                item["resurfaced"] = True
                item["canonical"] = True
                report.events.append(item)
            # split canonical / narrative copies: the CANONICAL chronicle
            # (admin/audit, hidden_audit) keeps every fact with its honest
            # title; the public `report.events` gets the court's version.
            # CALIBRATION NOTE (§22.10, run 2026-10-13): only the items
            # appended THIS turn (new_items) are political news — Fortune
            # rolls, conspiracies, resurfaced truth. The chronicler must not
            # touch the routine interval events (base_items), otherwise the
            # distortion-depth metric saturates near 1 and stops responding
            # to report.* constants. Canonical/admin copy stays complete.
            canonical_events = []
            for e in report.events:
                ce = {k: v for k, v in e.items() if k != "_severity"}
                ce["canonical"] = True
                ce.pop("narrative", None)
                canonical_events.append(ce)
            new_items = [{k: v for k, v in e.items() if k != "_severity"}
                         for e in report.events[canon_start:]]
            base_items = [dict(e) for e in report.events[:canon_start]]
            # per-civ grievance average feeds the flattery bias (§22.5)
            avg_g = politics_out.get("avg_grievance", {})
            mean_g = (sum(avg_g.values()) / len(avg_g)) if avg_g else 0.0
            distorted_new, audit_new = distort_chronicle(
                new_items,
                next((c.ruler for c in self.civilizations.values()
                      if c.active and c.ruler is not None), None),
                mean_g, self.politics.report)
            report.events = base_items + distorted_new
            report.fortune_events = [
                {k: v for k, v in e.items() if not k.startswith("_")}
                for e in new_items if e.get("kind") == "FortuneEvent"]
            # censorship buries some adverse news THIS turn — it goes to the
            # suppression queue and resurfaces later (§22.5 resurface rule)
            self.suppressed_chronicle.extend(audit_new.get("suppressed", []))
            report.narrative_distortion = {
                "applied": audit_new.get("applied", False),
                "fields": audit_new.get("fields", []),
                "touched_count": len(audit_new.get("fields", [])),
                "suppressed_count": len(audit_new.get("suppressed", [])),
                # exact denominator for the §22.10 «distortion depth» metric:
                # only items appended THIS turn (after canon_start) reach the
                # chronicler; hidden_audit["canonical_events"] is the WHOLE
                # turn chronicle and must not be used as the base (§22.5).
                "new_canonical": len(new_items),
                "bias": audit_new.get("bias", 0.0),
                "censorship": audit_new.get("censorship", 0.0),
            }
            report.hidden_audit["canonical_events"] = canonical_events
            report.hidden_audit["suppressed"] = audit_new.get("suppressed", [])

        # Steps 19–20 (§13): generate client representations from the finalized
        # report, then queue chronicle illustration tasks derived from them.
        report.view = build_turn_view(
            report, self.chronology, list(self.civilizations.values()))

        # Rev 4: ship the public political projections inside the client view
        # too (bands/facts only — exact loyalty/grievance/rolls never leave
        # the server, §22.8, test §22.9-2).
        if self.politics.enabled:
            report.view["politics"] = {
                "elites": report.elites,
                "conspiracies": report.conspiracies,
                "rulers": report.rulers,
                # §22.4/§22.8: bands and exporter names only — rival bids
                # never leave the server (hidden_audit["market_bids"]).
                "markets": report.markets,
                "influence": report.influence,
            }

        # §13 step 22: wrap the published numbers into the ruler's literary
        # turn report — wars, breakthroughs, discoveries, unrest, lawcraft —
        # one per civilization, generated server-side and shown verbatim.
        from .chronicle_report import build_ruler_report

        revisions_meta = []
        for snap in self._snapshot_list:
            for rev in snap.pending_amendments + snap.pending_repeals:
                revisions_meta.append({
                    "revision_id": rev.id, "law_id": rev.law_id,
                    "civilization_id": rev.civilization_id,
                    "action": rev.action.value,
                })
        folklore_meta = [
            {"folklore_id": item.id, "civilization_id": str(item.civilization_id),
             "title": item.title,
             "cancelled": bool(getattr(item, "cancelled", False))}
            for item in self.folklore
        ]
        report.ruler_reports = build_ruler_report(
            report.view,
            {
                "years_elapsed": report.years_elapsed,
                "era_before": report.era_before.value,
                "era_after": report.era_after.value,
                "world_year_after": report.world_year_after,
                "applied_revisions": report.applied_revisions,
                "_revisions_meta": revisions_meta,
                "_civ_names": {cid: (c.name or cid)
                               for cid, c in self.civilizations.items()},
                "_war_parties": war_parties,
            },
            [{"id": cid, "name": c.name}
             for cid, c in self.civilizations.items() if c.active],
            folklore_meta,
        )
        # The literary reports belong to the published client view as well:
        # GET /turns/report ships `view` verbatim (thin client shows them).
        report.view["ruler_reports"] = report.ruler_reports

        tasks = queue_image_tasks(report.view, self.world_id, registry=self.tokens)
        # §13 step 20: keep the created tasks server-side so renderers can
        # claim/complete them and clients can poll progress via the API.
        self.image_queue.extend(tasks)
        report.image_tasks = [t.to_json() for t in tasks]

        # Rev 4 audit consumers (admin raw endpoint) read the last finalized
        # report including hidden_audit / canonical chronicle (§22.6 step 21).
        self.last_report = report
        return report

    # ------------------------------------------------------------- internals

    def _run_markets_step(self, turn: Turn, span: SimDuration,
                          economy: dict) -> dict:
        """Rev 4 step 16a (§22.4, TASK-074): markets, niches, influence fields.

        Runs BEFORE the elite step so this turn's market losses can feed
        faction grievance in 17a. Every draw comes from ``WorldRng`` keyed
        by (seed, turn number, niche, civ) — same seed & history ⇒ same
        market shifts (§22.9-8/10). Rival bids stay server-side; only
        saturation bands and exporter names are projected publicly (§22.8).

        State lives on the engine (``niche_map``, ``influence_regions``,
        ``_internal_rulers``) and persists between turns exactly like the
        rest of the canonical world state.
        """
        from . import markets as MK
        from .chronology import risk_premium_value

        if not self.niche_map:
            self.niche_map = MK.build_default_niche_map()
            self.influence_regions = MK.build_influence_map()
        if not hasattr(self, "_treaties"):
            self._treaties: dict[tuple[str, str], float] = {}

        active_ids = sorted(cid for cid, c in self.civilizations.items()
                            if c.active)
        # --- internal rulers (§22.4 preamble): derived from public RulerState
        rulers: dict[str, MK.InternalRuler] = {}
        for cid in active_ids:
            civ = self.civilizations[cid]
            cap = max(0.05, civ.population / 1000.0 * (0.5 + civ.progress))
            rulers[cid] = MK.make_internal_ruler(cid, civ.ruler, cap)
        self._internal_rulers = rulers

        # --- trade interest: sectors a civ exports into (§22.4-1). Derived
        # deterministically from the economy profile: the strongest sectors
        # above the mean share become that civ's export lanes.
        from .elites import DEFAULT_ECONOMY_PROFILE
        trade_interest: dict[str, list[str]] = {}
        for cid in active_ids:
            prof = {**DEFAULT_ECONOMY_PROFILE,
                    **(self.civilizations[cid].economy_profile or {})}
            mean = sum(prof.values()) / max(len(prof), 1)
            strong = [s for s, w in sorted(prof.items()) if w > mean]
            sectors = []
            for lane in strong:
                if "agri" in lane or "land" in lane:
                    sectors += ["grain"]
                elif lane == "trade":
                    sectors += ["textiles", "spices"]
                elif lane == "craft":
                    sectors += ["textiles", "metals"]
                elif "military" in lane:
                    sectors += ["horse", "metals"]
                elif lane in ("religion", "administration", "writing"):
                    sectors += ["timber"]      # scriptoria, temples, shipyards
            trade_interest[cid] = sorted(set(sectors)) or ["grain"]

        # --- risk premiums (§22.3 formula, same inputs as step 6): the dearer
        # doing business feels, the lower the stakes rivals carry into it.
        eq = self.politics.equilibrium
        risk_premiums = {cid: risk_premium_value(
            self.civilizations[cid].stability_years, eq.risk_base, eq.tau_law)
            for cid in active_ids}

        out = MK.resolve_markets(self.rng, turn.number, self.niche_map,
                                 rulers, trade_interest, risk_premiums)

        # --- first-mover entry (§22.4-2): every exporter with an admin budget
        # may buy its way into a free niche; cost grows geometrically with
        # saturation, so a hesitating player pays the full risk price later.
        entries: list[dict] = []
        for cid in active_ids:
            civ = self.civilizations[cid]
            for niche_id, niche in sorted(self.niche_map.items()):
                if niche.sector not in trade_interest[cid]:
                    continue
                if cid in niche.entrants or not niche.is_open():
                    continue
                cost = niche.entry_cost(risk_premiums[cid])
                if civ.administrative_budget >= cost:
                    civ.administrative_budget -= cost
                    niche.entrants[cid] = max(
                        0.05, civ.population / 1000.0 * (0.5 + civ.progress))
                    entries.append({"civ": cid, "niche": niche_id,
                                    "cost": round(cost, 3)})

        # --- influence field drift (§22.4-3): slow presence growth, treaties
        # and folklore export nudge neighbours' fields. No instant conquests.
        treaties = [(a, b) for a, b in self._active_treaty_pairs(active_ids)]
        folklore_export: dict[str, float] = {}
        for item in self.folklore[-12:]:
            owner = getattr(item, "owner_civ_id", None) or \
                getattr(item, "civilization_id", None)
            if owner in active_ids:
                folklore_export[owner] = (folklore_export.get(owner, 0.0) + 1.0)
        MK.update_influence(self.influence_regions, active_ids, span.years,
                            treaties, folklore_export)

        # --- diplomacy with character (§22.4-4): ONE hidden break roll per
        # treaty per turn; tyrants break their word more often.
        broken = []
        for pair in treaties:
            fact = MK.maybe_break_treaty(self.rng, turn.number, pair, rulers)
            if fact is not None:
                self._treaties.pop(tuple(sorted(pair)), None)
                broken.append(fact)

        return {"shifts": out["shifts"], "public": out["public"],
                "hidden": out["hidden"], "grievances": out["grievances"],
                "entries": entries, "broken_treaties": broken}

    def _active_treaty_pairs(self, active_ids: list[str]):
        """Treaty registry (§22.4-3): bonds of long presence, formed
        deterministically between consecutive active civilizations and kept
        on the engine until one side breaks its word (§22.4-4) or dies out.
        No scripts, no player API — only game-time facts."""
        reg = self._treaties
        for k in [k for k in reg if not all(c in active_ids for c in k)]:
            reg.pop(k, None)                     # dead civ voids its treaties
        ids = list(active_ids)
        for i in range(0, len(ids) - 1, 2):
            a, b = sorted((ids[i], ids[i + 1]))
            reg.setdefault((a, b), 0.0)
        return list(reg)

    def _run_politics_step(self, turn: Turn, span: SimDuration,
                           economy: dict,
                           market_grievances: dict[str, float] | None = None
                           ) -> dict:
        """Rev 4 steps 17a/17b (§22.2–22.3): elites → conspiracy → equilibrium.

        Returns a bundle consumed by the TurnReport (public projections +
        server-side hidden audit). Deterministic in every input: same seed,
        same laws, same spans ⇒ same rolls and successions (§22.9-1).
        """
        from . import elites as EL
        from .distortion import distort_chronicle

        cfg = self.politics
        year = self.chronology.current_year
        out_public: dict[str, list] = {"elites": [], "conspiracies": [],
                                       "rulers": []}
        hidden: dict = {"elite_deltas": {}, "conspiracy_audit": {},
                        "successions": []}
        avg_grievance: dict[str, float] = {}
        elites_by_civ: dict[str, list] = {}

        for civ_id, civ in self.civilizations.items():
            if not civ.active:
                continue
            if civ.ruler is None:
                civ.ruler = EL.ensure_ruler(civ_id, civ.name, year)
            if civ_id not in self.factions:
                self.factions[civ_id] = EL.form_factions(
                    civ_id, civ.economy_profile)
            factions = self.factions[civ_id]

            # tax pressure proxy: effective extraction this interval.
            # The admin budget spent on decrees IS the extraction felt by
            # the elites; normalized against annual production it becomes
            # civ.tax_load (0..1), which then persists between turns and
            # reverts to its centre in step 17b (§22.3 equilibrium).
            prod = economy.get(civ_id, {}).get("production", 0.0)
            spent = sum(self._charged_costs_by_civ.get(civ_id, []))
            load_now = min(1.0, spent / max(prod, 1e-9)) if prod > 0 else 0.0
            civ.tax_load = max(civ.tax_load, load_now)   # shocks spike it now
            deltas = EL.update_factions(factions, civ.ruler, span.years,
                                        civ.tax_load, cfg)
            # §22.4-1: rivals that lost market shares this turn carry the
            # loss into their grievance — trade orders feel it most.
            if market_grievances:
                mg = market_grievances.get(civ_id, 0.0)
                if mg > 0.0:
                    for f in factions:
                        bump = mg * 0.05 * (1.6 if f.name == "merchants" else 1.0)
                        f.grievance = min(1.0, f.grievance + bump)
            hidden["elite_deltas"][civ_id] = deltas

            # cruelty decays slowly; fresh cruelty still protects the throne
            civ.ruler.cruelty_accumulator = max(
                0.0, civ.ruler.cruelty_accumulator
                - cfg.elite.cruelty_decay_per_year * span.years)
            EL.drift_style(civ.ruler, factions, span.years, cfg)

            # §22.3 ву-вэй: a quiet turn (no legal edits) accumulates dao
            # power and calms elite nerves — conspiracy pressure is reduced.
            edits_this_turn = any(
                snap.civilization_id == civ_id
                and (snap.pending_amendments or snap.pending_repeals)
                for snap in self._snapshot_list)
            wuwei_bonus = 0.0
            if not edits_this_turn:
                civ.ruler.dao_balance += cfg.equilibrium.dao_wuwei_per_year \
                    * (1.0 + civ.stability_years / max(cfg.equilibrium.tau_law, 1.0)) \
                    * span.years
                wuwei_bonus = min(0.5, civ.ruler.dao_balance)

            # statecraft grows from balanced rule: low grievance average and
            # moderate aggregates earn prudence (§22.1-4 «добродетель»)
            g_avg = (sum(f.grievance for f in factions) / len(factions)
                     if factions else 0.0)
            avg_grievance[civ_id] = g_avg
            if g_avg < 0.35 and abs(civ.militarization - 0.15) < 0.3:
                civ.ruler.statecraft = min(
                    1.0, civ.ruler.statecraft
                    + 0.001 * span.years * (1.0 + civ.ruler.dao_balance))

            # --- step 17a: conspiracy check ---------------------------------
            pressure = EL.conspiracy_pressure(factions, cfg.conspiracy.threshold)
            pressure -= wuwei_bonus * 0.2          # quiet rule soothes plotters
            if pressure > 0.0:
                res = EL.resolve_conspiracy(
                    self.rng, turn.number, civ_id, civ.ruler, factions,
                    pressure, year, cfg)
                ev, audit = res[0], res[1]
                new_ruler = res[2] if len(res) > 2 else None
                if ev is not None:
                    self.conspiracies.append(ev)
                    hidden["conspiracy_audit"][ev.id] = audit
                    out_public["conspiracies"].append(ev.to_chronicle_json())
                    if new_ruler is not None:
                        # mechanical succession: civ & laws survive, ruler
                        # changes; player has NO API to pick an heir (#2)
                        civ.ruler = new_ruler
                        hidden["successions"].append({
                            "civ_id": civ_id, "new_ruler": new_ruler.name,
                            "origin": new_ruler.origin_faction})
                        # treasury confiscation feeds the admin budget loss
                        civ.administrative_budget *= 0.7

            # --- step 17b: equilibrium revert of extreme aggregates ---------
            eq = cfg.equilibrium
            for attr, center in eq.centers.items():
                civ.revert_toward(attr, center, eq.revert_rate, span.years)
            # modesty: loud display taxes merchants/artisans indirectly (§22.3)
            modesty_line = eq.modesty_threshold_scale * max(civ.progress, 0.05)
            if civ.prestige_display > modesty_line:
                over = civ.prestige_display - modesty_line
                for f in factions:
                    if f.name in ("merchants", "artisans"):
                        f.grievance = min(1.0, f.grievance + over * 0.1 * span.years * 0.02)

            pub_factions = [dict(f.to_public_json(), civilization_id=civ_id)
                            for f in factions]
            out_public["elites"].extend(pub_factions)
            elites_by_civ[civ_id] = pub_factions
            style = civ.ruler.style
            band = ("tyrannic" if style > 0.5 else
                    "hard-handed" if style > 0.15 else
                    "liberal" if style < -0.15 else "moderate")
            out_public["rulers"].append({
                "civilization_id": civ_id, "name": civ.ruler.name,
                "style_band": band,               # range, never exact number
                "public_virtues": round(civ.ruler.statecraft, 2),
                "canonical": True})

        return {"public": {**out_public, "elites_by_civ": elites_by_civ},
                "hidden": hidden,
                "avg_grievance": avg_grievance}

    def _language_fragmentation(self, civ_id: str) -> float:
        """TASK-068: how poorly decrees travel through one civilization.

        0.0 when the civ owns no settlements (a bare abstract world keeps
        the pre-TASK-068 price formula byte-identical); otherwise the share
        of the population whose local speech barely understands the koine.
        Pure function of game state — never of wall-clock time (§20.4).
        """
        if not any(s.civilization_id == civ_id
                   for s in self.language.settlements):
            return 0.0
        return max(0.0, 1.0 - self.language.average_intelligibility_to_koine(civ_id))

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
        turn_id_of_application: str | None = None,
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
                        payload={"law_id": law.id,
                                 "created_by_revision": rev.id},
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
                                     "final_status": LawStatus.ACTIVE.value,
                                     "created_by_revision": rev.id},
                        ))
                case LawUserAction.CLARIFY_LAW:
                    law.raw_text = rev.raw_text
          except Exception as exc:      # §3.4 Failed — isolate this revision
            failed[rev.id] = str(exc)
            continue
          rev.simulation_turn_id = turn_id_of_application
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
