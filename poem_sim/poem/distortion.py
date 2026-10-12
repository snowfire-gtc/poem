"""Rev 4 §22.5 — reports with distortion: flattery and censorship.

TASK-076 (core layer). «Around a tyrant everyone is silent or flattering.»

Contract (hard design constraints):

 * CANONICAL NUMBERS STAY HONEST. Every structured figure the API ships in
   dashboard/economy fields is untouched; audits and tests run on them.
 * ONLY the narrative layer is distorted: favorable events are inflated,
   unfavorable ones softened or dropped from this turn's chronicle ("the
   revolt nobody reported shows up later as tax arrears in canonical
   figures").
 * Distorted payloads MUST carry ``"narrative": true``; honest ones carry
   ``"canonical": true`` so even a thin client can show the label (§22.5).

The distortion is deterministic: severity is a pure function of the ruler's
style, censorship level and self_deception — no wall clock, no randomness
beyond the seeded world RNG if one is passed explicitly.
"""

from __future__ import annotations

from .core import RulerState

#: Event kinds the court chronicler prefers to soften or bury (§22.5).
ADVERSE_KINDS = {"FortuneEvent", "Repeal", "Suspension", "TreatyExpired"}
ADVERSE_TITLE_HINTS = ("неурожай", "эпидемия", "наводнение", "землетрясение",
                       "волнение", "смута", "бунт")


def flattery_bias(style: float, grievance_level: float, scale: float) -> float:
    """§22.5: how strongly the court paints reality brighter, −1..+1 axis.

    A liberal court (style < 0) barely distorts; a tyrannic one distorts
    hard, and widespread hidden grievance makes courtiers lie MORE (they
    fear bad news reaching the throne)."""
    tyranny = max(0.0, style)
    return scale * tyranny * (1.0 + grievance_level)


def censorship_level(style: float, base: float) -> float:
    """Probability band width for dropping adverse items entirely."""
    tyranny = max(0.0, style)
    return min(0.95, base * tyranny)


def risk_estimate_reported(risk_true: float, bias: float,
                           self_deception: float) -> float:
    """§22.5: reported risk drifts below true risk at a tyrannic court; the
    ruler who believes his own reports (self_deception) discounts further —
    the trap of the throne."""
    return max(0.0, risk_true - bias * 0.5 - self_deception * risk_true)


def distort_chronicle(events: list[dict], ruler: RulerState | None,
                      avg_grievance: float, params) -> tuple[list[dict], dict]:
    """Step 19a: produce the NARRATIVE copy of the turn chronicle.

    Returns (distorted_events, audit). The input list is never mutated —
    callers keep the canonical version for admin/audit endpoints. Items
    removed by censorship are recorded in audit["suppressed"] so they may
    resurface later ("скрытая правда", §22.5).
    """
    if ruler is None:
        return list(events), {"applied": False, "fields": []}
    bias = flattery_bias(ruler.style, avg_grievance, params.flattery_scale)
    censor = censorship_level(ruler.style, params.censorship_level)
    out: list[dict] = []
    suppressed: list[dict] = []
    touched: list[str] = []
    for i, ev in enumerate(events):
        adverse = _is_adverse(ev)
        if adverse and censor > 0.0 and _censor_slot(i, len(events)) < censor:
            suppressed.append(ev)          # not reported THIS turn
            continue
        item = dict(ev)
        if adverse and bias > 0.0:
            item["title"] = _soften(item.get("title", ""))
            item["detail"] = (item.get("detail", "") +
                              " Так доложил придворный хронист.")
            touched.append(f"events[{i}]")
        elif not adverse and bias > 0.0:
            item["detail"] = (item.get("detail", "") +
                              " В летописи это подано ярче, чем было.").strip()
            touched.append(f"events[{i}]")
        item["narrative"] = True
        out.append(item)
    audit = {
        "applied": bool(touched or suppressed),
        "fields": touched,
        "suppressed": suppressed,
        "bias": round(bias, 4),
        "censorship": round(censor, 4),
    }
    if ruler.style > 0.3:
        # believing one's own flattery erodes judgment (§22.5 self_deception)
        ruler.self_deception = min(1.0, ruler.self_deception +
                                   params.self_deception_gain)
    return out, audit


def _is_adverse(ev: dict) -> bool:
    kind = ev.get("kind", "")
    title = str(ev.get("title", "")).lower()
    if kind in ADVERSE_KINDS:
        return any(h in title for h in ADVERSE_TITLE_HINTS) or kind == "FortuneEvent"
    return False


def _censor_slot(index: int, total: int) -> float:
    """Deterministic pseudo-position in [0,1): censorship order depends on
    the item index only — reproducible without touching the RNG stream."""
    return ((index * 2654435761) % 1000) / 1000.0 if total else 0.0


SOFTEN_MAP = {
    "Неурожай": "Скудный, но достойный урожай",
    "Эпидемия": "Лёгкая немилость погоды",
    "Наводнение": "Разливание рек к вящему плодородию",
    "Землетрясение": "Лёгкое волнение земли",
    "Народное волнение": "Выражение народной преданности",
}


def _soften(title: str) -> str:
    for raw, pretty in SOFTEN_MAP.items():
        if raw.lower() in title.lower():
            return pretty
    return title + " (приуменьшено докладчиком)"
