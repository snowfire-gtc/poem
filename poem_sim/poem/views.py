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
    """A queued illustration request for one turn (§13 step 20)."""

    id: str
    world_id: str
    turn_id: str
    prompt: str
    status: str = "queued"          # queued | running | done | failed
    payload: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "world_id": self.world_id,
            "turn_id": self.turn_id,
            "prompt": self.prompt,
            "status": self.status,
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
        "admin_costs": report.admin_costs,
        "applied_revisions": report.applied_revisions,
        "deferred_revisions": report.deferred_revisions,
        "superseded_revisions": report.superseded_revisions,
        "expired_effects": report.expired_effects,
        "notices": list(report.notices),
    }


def render_dashboard_text(view: dict) -> str:
    """Plain-text dashboard block in the exact shape of §16.1."""
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
