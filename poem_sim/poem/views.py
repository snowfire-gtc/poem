"""Turn report views & image-generation task queue.

§13 pipeline steps 19–20:
    19. Generate representations (client-facing projections of the raw
        simulation state — never the raw state itself);
    20. Queue image-generation tasks (chronicle illustrations for the turn).

Design rules honoured here:
 * Views are derived from the FINALIZED turn report and the server-side
   chronology, so every client sees the same deterministic projection (§4.1).
 * The dashboard block follows §16.1 exactly: turn number, era, elapsed game
   time, current world year and the projected span of the NEXT turn, plus a
   warning line when the interval changes because civilizations advanced.
 * Image jobs are stored server-side as plain dicts; they reference only
   data already published in the view (turn number, era, year), so rendering
   is reproducible from the chronology alone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class ImageTask:
    """A queued illustration request for one turn (§13 step 20).

    Tasks are consumed only by renderer workers running on the server's own
    video cards; nothing is ever delegated to external generation services.
    ``claimed_by``/``lease_expires_at`` implement a lease so a crashed
    worker's task returns to the queue instead of being lost.
    """

    id: str
    world_id: str
    turn_id: str
    prompt: str
    status: str = "queued"          # queued | running | done | failed
    claimed_by: str | None = None   # local GPU worker holding the lease
    lease_expires_at: float | None = None  # unix ts; None when not running
    asset_ref: str | None = None    # server-local path, filled on success
    payload: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "world_id": self.world_id,
            "turn_id": self.turn_id,
            "prompt": self.prompt,
            "status": self.status,
            "claimed_by": self.claimed_by,
            "lease_expires_at": self.lease_expires_at,
            "asset_ref": self.asset_ref,
            "payload": self.payload,
        }


# Era names for the dashboard (§7.3, display layer only).
ERA_LABELS_RU = {
    "dawn": "Заря",
    "ancient": "Древняя",
    "classical": "Классическая",
    "medieval": "Средневековье",
    "renaissance": "Возрождение",
    "industrial": "Индустриальная",
    "modern": "Современная",
    "atomic": "Атомная",
    "information": "Информационная",
    "planetary": "Планетарная",
}


def _era_label(era_value: str) -> str:
    return ERA_LABELS_RU.get(era_value, era_value.capitalize())


def build_turn_view(report, chronology, civilizations) -> dict:
    """Deterministic client projection of a finalized TurnReport (§16).

    ``report`` is a :class:`poem.turn.TurnReport`, ``chronology`` the
    post-turn :class:`WorldChronology` and ``civilizations`` the list used
    to project the next turn's span.
    """
    from .chronology import calculate_turn_span  # local import: avoid cycle

    next_span = calculate_turn_span(chronology, list(civilizations))
    dashboard = {
        "turn": report.turn_number,
        "era": _era_label(report.era_after.value),
        "era_value": report.era_after.value,
        "years_since_last_turn": report.years_elapsed,
        "current_year": chronology.current_year,
        "next_turn_span_years": next_span.years,
    }

    laws_section = []
    for snap in report.snapshots:
        for law in snap.active_laws:
            laws_section.append({
                "id": law.id,
                "civilization_id": snap.civilization_id,
                "title": law.title,
                "status": law.status.value,
                "tradition": round(law.tradition, 4),
                "age_years": law.age_years,
                "conflicts": [c.description for c in snap.conflicts
                              if law.id in (c.law_a, c.law_b)],
            })

    economy_section = [
        {"civilization_id": civ_id, **{k: round(v, 4) for k, v in metrics.items()}}
        for civ_id, metrics in report.economy.items()
    ]

    folklore_section = [
        {"folklore_id": fid, **{k: round(v, 4) for k, v in m.items()}}
        for fid, m in report.folklore.items()
    ]

    return {
        "dashboard": dashboard,
        "laws": laws_section,
        "economy": economy_section,
        "folklore": folklore_section,
        "prejudices": [
            {"prejudice_id": pid, **{k: round(v, 4) for k, v in m.items()}}
            for pid, m in report.prejudices.items()
        ],                                    # §13 step 15
        "diplomacy": list(report.diplomacy),  # §13 step 16 (§11.6 game time)
        "wars": [
            {"war_id": wid, **m} for wid, m in report.wars.items()
        ],                                    # §13 step 17
        "admin_costs": report.admin_costs,
        "applied_revisions": report.applied_revisions,
        "deferred_revisions": report.deferred_revisions,
        "superseded_revisions": report.superseded_revisions,
        "expired_effects": report.expired_effects,
        "events": getattr(report, "events", []),   # §19.1 chronicle
        "notices": list(report.notices),
    }


# ---------------------------------------------------------------------------
# Interval events — chronicle entries covering one turn span (§19.1, §13 step 19)
# ---------------------------------------------------------------------------

# Rotating deterministic templates for cultural milestones. Selection depends
# only on the world year (game time), never on wall-clock time (§20.4).
_MILESTONE_TITLES = (
    "Смена поколений устных рассказчиков",
    "Новые песни о старых законах",
    "Праздник урожая в главных поселениях",
    "Летописцы фиксируют обычаи округи",
    "Мастеровые гильдии передают ремесло детям",
)


def interval_events(report, world_year_before: float) -> list:
    """Build the turn's chronicle (§19.1).

    Key events always appear as chronicle entries: era change, repealed or
    suspended laws, expired treaties/transitions/envoy arrivals. For long
    intervals, evenly spaced culture/folklore milestones are added so that a
    40-year ancient turn reads as a story instead of a time jump; short turns
    (< MIN_EVENT_SPAN_YEARS) get none — small yearly processes are not faked
    frame-by-frame ("не все процессы масштабировать линейно").
    """
    from .core import (
        EVENT_INTERVAL_YEARS,
        IntervalEvent,
        MIN_EVENT_SPAN_YEARS,
        TimedEffectKind,
    )

    events: list[IntervalEvent] = []
    end_year = world_year_before + report.years_elapsed

    # --- key events -------------------------------------------------------
    if report.era_after != report.era_before:
        events.append(IntervalEvent(
            kind="EraChange", year=end_year,
            title=f"Наступает {_era_label(report.era_after.value)} эпоха",
            detail=f"Мир переходит от {_era_label(report.era_before.value)} "
                   f"к {_era_label(report.era_after.value)}."))

    for snap in report.snapshots:
        for rev in snap.pending_repeals:
            events.append(IntervalEvent(
                kind="Repeal", year=end_year,
                title="Закон отменён",
                detail=f"{rev.raw_text[:80]}"))
        for law_id in snap.suspended_laws:
            events.append(IntervalEvent(
                kind="Suspension", year=end_year,
                title="Закон приостановлен",
                detail=f"law {law_id}: действие заморожено на переходный срок"))

    for eff in getattr(report, "expired_effect_details", []):
        kind = eff["kind"]
        year = min(max(eff.get("year", end_year), world_year_before), end_year)
        if kind == TimedEffectKind.TREATY.value:
            events.append(IntervalEvent(kind="TreatyExpired", year=year,
                                        title="Договор истёк",
                                        detail=eff.get("detail", "")))
        elif kind == TimedEffectKind.TRAVEL.value:
            events.append(IntervalEvent(kind="TravelArrival", year=year,
                                        title="Посол прибыл",
                                        detail=eff.get("detail", "")))
        elif kind == TimedEffectKind.LAW_TRANSITION.value:
            events.append(IntervalEvent(kind="TransitionEnd", year=year,
                                        title="Переходный период завершён",
                                        detail=eff.get("detail", "")))

    # --- intermediate cultural milestones (long intervals only) -----------
    if report.years_elapsed >= MIN_EVENT_SPAN_YEARS:
        n_milestones = int(report.years_elapsed // EVENT_INTERVAL_YEARS)
        # cap density: even marathon-speed ancient turns stay readable
        n_milestones = min(n_milestones, max(1, int(report.years_elapsed // 20)))
        for i in range(1, n_milestones + 1):
            frac = i / (n_milestones + 1)
            year = round(world_year_before + report.years_elapsed * frac, 1)
            idx = int(year) % len(_MILESTONE_TITLES)
            events.append(IntervalEvent(
                kind="CulturalMilestone", year=year,
                title=_MILESTONE_TITLES[idx],
                detail="Промежуточная веха фольклора и культуры за этот ход"))

    events.sort(key=lambda e: (e.year, e.kind))
    return events


def render_dashboard_text(view: dict) -> str:
    """Plain-text dashboard block in the exact shape of §16.1, followed by
    the turn chronicle (§19.1: key events shown as a chronicle)."""
    d = view["dashboard"]
    lines = [
        f"Ход: {d['turn']}",
        f"Эпоха: {d['era']}",
        f"Прошло времени с прошлого хода: {d['years_since_last_turn']:g} лет",
        f"Текущий год: {d['current_year']:g}",
        f"Следующий ход охватит примерно: {d['next_turn_span_years']:g} лет",
    ]
    for notice in view.get("notices", []):
        if notice.startswith("Внимание"):
            lines.append(notice)
    events = view.get("events") or []
    if events:
        lines.append("Хроника хода:")
        for ev in events:
            lines.append(f"[{ev['year']:g}] {ev['title']} — {ev['detail']}")
    return "\n".join(lines)


def queue_image_tasks(view: dict, world_id: str) -> list[ImageTask]:
    """§13 step 20: create illustration tasks from the generated view.

    One hero image per turn (chronicle scene of the era/year) plus one
    smaller vignette per applied law revision, so the report reads like a
    medieval/ancient chronicle even when a whole generation passed (§19.1).
    """
    d = view["dashboard"]
    tasks: list[ImageTask] = [
        ImageTask(
            id=str(uuid.uuid4()),
            world_id=world_id,
            turn_id=str(d["turn"]),
            prompt=(
                f"Chronicle illustration: {_era_label(d['era_value'])} era, "
                f"world year {d['current_year']:g}, turn {d['turn']}"
            ),
            payload={"kind": "hero", "era": d["era_value"],
                     "year": d["current_year"]},
        )
    ]
    for rev_id in view.get("applied_revisions", []):
        tasks.append(ImageTask(
            id=str(uuid.uuid4()),
            world_id=world_id,
            turn_id=str(d["turn"]),
            prompt=f"Law change vignette for revision {rev_id}",
            payload={"kind": "law_vignette", "revision_id": rev_id},
        ))
    return tasks
