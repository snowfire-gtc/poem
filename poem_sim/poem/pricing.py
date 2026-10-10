"""Cost model for law changes (§5) — with time-of-day penalties REMOVED.

TASK-060: the previous revision of the design contained
``late_edit_cost_multiplier`` and ``late_edit_delay_chance`` tied to real
wall-clock time of day. They are deleted here; nothing in this module reads
the hour/minute of any timestamp.

Formula (§5.1):

    law_change_cost =
        base_change_cost
        + complexity_delta
        + scope_delta
        + tradition_level * tradition_penalty
        + contradiction_penalty
        + administrative_overload_penalty

There is deliberately NO ``late_edit_penalty`` term.
"""

from __future__ import annotations

from dataclasses import dataclass

from .core import Law, LawUserAction


@dataclass(frozen=True)
class PricingConfig:
    base_change_cost: float = 10.0
    complexity_unit_cost: float = 4.0          # per unit of law complexity
    scope_unit_cost: float = 2.0               # per normalized change volume
    tradition_penalty: float = 8.0             # multiplied by tradition level
    contradiction_penalty: float = 6.0         # folklore/legal conflicts
    admin_overload_unit_cost: float = 3.0      # bureaucracy strain
    transition_cost_per_year: float = 0.5      # §5.2 transitional period
    urgent_action_cost: float = 5.0            # special-resource urgency,
                                               # NOT wall-clock related (§5.2)

    # NOTE: no late_edit_cost_multiplier / late_edit_delay_chance fields.
    # Any attempt to add a time-of-day dependent field should be rejected
    # in code review per §20 rule 5.


_ACTION_WEIGHT = {
    LawUserAction.AMEND_LAW: 1.0,
    LawUserAction.SUBMIT_DRAFT: 1.0,
    LawUserAction.CLARIFY_LAW: 0.4,
    LawUserAction.SUSPEND_LAW: 0.7,
    LawUserAction.REINSTATE_LAW: 0.6,
    LawUserAction.REPEAL_LAW: 1.3,
    LawUserAction.CANCEL_PENDING_CHANGE: 0.1,
}


def cost_of_law_change(
    law: Law,
    action: LawUserAction,
    *,
    cfg: PricingConfig,
    scope_delta: float = 1.0,
    contradictions: int = 0,
    administrative_overload: float = 0.0,
    transition_years: float = 0.0,
    urgent: bool = False,
    active_laws: "list | tuple | None" = None,
) -> float:
    """Compute the gameplay cost of a confirmed law edit.

    All inputs are *game-state* values. No datetime argument exists on
    purpose: cost cannot depend on when the player sent the edit (§2).
    """
    weight = _ACTION_WEIGHT.get(action, 1.0)
    age_factor = 1.0 + min(law.age_years / 200.0, 1.0) * 0.5  # older laws: harder
    tradition_level = law.tradition * age_factor              # укоренённость × возраст

    # Bureaucracy strain (§5: «уровень бюрократии») — the more active laws a
    # civilization maintains, the heavier each additional edit lands on the
    # administration. Derived purely from GAME state; no clock input exists
    # here by design (§2, §20.5).
    bureaucracy_level = max(len(active_laws or ()) - 1, 0)

    cost = (
        cfg.base_change_cost                                      # base_change_cost
        + cfg.complexity_unit_cost * law.complexity * weight      # complexity_delta
        + cfg.scope_unit_cost * scope_delta                       # scope_delta
        + tradition_level * cfg.tradition_penalty                 # tradition term
        + contradictions * cfg.contradiction_penalty              # contradiction_penalty
        + (bureaucracy_level + law.administrative_load + administrative_overload)
        * cfg.admin_overload_unit_cost                            # admin overload
        + transition_years * cfg.transition_cost_per_year         # transitional period
    )
    if urgent:
        # Urgency as an in-game action spending a special resource (§5.2),
        # never tied to real time of day.
        cost += cfg.urgent_action_cost
    return round(cost, 4)
