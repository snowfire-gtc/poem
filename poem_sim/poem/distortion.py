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

    Scope (§22.5 calibration note, run 2026-10-13): the chronicler only
    touches items carrying political news — Fortune events, conspiracy
    outcomes, resurfaced truth (``kind == "FortuneEvent"`` or an explicit
    ``"conspiracy_id"`` field). Routine interval entries (breakthroughs,
    treaties, market notes) pass through untouched: flattering mundane
    facts adds nothing to the court's lie, while it saturates the
    distortion-depth metric (§22.10) near 1 and makes ``report.*``
    constants unresponsive.
    """
    if ruler is None:
        return list(events), {"applied": False, "fields": []}
    bias = flattery_bias(ruler.style, avg_grievance, params.flattery_scale)
    censor = censorship_level(ruler.style, params.censorship_level)
    #: §22.5 hard contract: the fait accompli — a toppled ruler — can be
    #: SOFTENED by the court ("добровольное отречение") but never buried.
    #: A suppressed overthrow would erase the succession itself from the
    #: chronicle, breaking the canonical-facts-reach-the-players guarantee
    #: (§22.2.3, test §22.9-4). Censorship still applies to every other
    #: adverse item (Fortune shocks, exposed plots). The exemption is
    #: title-based so it works for both raw ConspiracyEvents and the
    #: chronicle entries turn.py stamps with the honest overthrow title.
    unsuppressable = lambda ev: (
        ev.get("outcome") == "ruler_overthrown"
        or "свержение правителя" in str(ev.get("title", "")).lower())
    out: list[dict] = []
    suppressed: list[dict] = []
    touched: list[str] = []
    for i, ev in enumerate(events):
        if not _is_political(ev):
            item = dict(ev)              # routine news: honest as-is
            out.append(item)
            continue
        adverse = _is_adverse(ev)
        if (adverse and censor > 0.0 and not unsuppressable(ev)
                and _censor_slot(i, len(events)) < censor):
            suppressed.append(ev)          # not reported THIS turn
            continue
        item = dict(ev)
        if adverse and bias > 0.0:
            # keep the honest wording alongside the softened one — thin
            # clients and audits can still pin the fact (§22.5 label rule)
            honest = item.get("title", "")
            item["honest_title"] = honest
            item["title"] = _soften(honest)
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


#: Chronicle kinds that carry political news — the court lies about these.
POLITICAL_KINDS = {"FortuneEvent", "ConspiracyEvent"}


def _is_political(ev: dict) -> bool:
    """True if the item is political news the chronicler may distort.

    Fortune events and conspiracy outcomes (§22.1/§22.2.3), plus items
    already marked ``resurfaced`` (delayed truth from the suppression
    queue, §22.5). Everything else — breakthroughs, treaties, market
    notes — is routine chronicle material and stays canonical."""
    kind = ev.get("kind", "")
    if kind in POLITICAL_KINDS or ev.get("conspiracy_id") is not None:
        return True
    return bool(ev.get("resurfaced"))


def _is_adverse(ev: dict) -> bool:
    kind = ev.get("kind", "")
    title = str(ev.get("title", "")).lower()
    if kind == "ConspiracyEvent" or ev.get("conspiracy_id") is not None:
        # a plot, exposed or successful, is bad news for the throne (§22.2.3)
        return True
    if kind in ADVERSE_KINDS:
        return any(h in title for h in ADVERSE_TITLE_HINTS) or kind == "FortuneEvent"
    return False


def _censor_slot(index: int, total: int) -> float:
    """Deterministic pseudo-position in [0,1): censorship order depends on
    the item index only — reproducible without touching the RNG stream."""
    return ((index * 2654435761) % 1000) / 1000.0 if total else 0.0


#: Entries whose softened form still contains the original substring
#: (prefix-style rewording). They must be tried LAST, otherwise a shorter
#: key would shadow a longer honest fact that merely embeds it (§22.5).
_SOFTEN_PREFIXES = {
    "Народное волнение": "Выражение народной преданности",
}

SOFTEN_MAP = {
    "Неурожай": "Скудный, но достойный урожай",
    "Эпидемия": "Лёгкая немилость погоды",
    "Наводнение": "Разливание рек к вящему плодородию",
    "Землетрясение": "Лёгкое волнение земли",
    # §22.2.3: a toppled tyrant is reported as a peaceful abdication —
    # the court never admits the throne was taken by force
    "Свержение правителя": "Добровольное отречение государя",
    "Заговор раскрыт": "Небольшое недоразумение при дворе",
    **_SOFTEN_PREFIXES,
}


def _soften(title: str) -> str:
    # exact-replacement entries first; prefix-style ones only if nothing
    # else matched (their softened text still contains the raw key)
    for mapping in (
        {k: v for k, v in SOFTEN_MAP.items() if k not in _SOFTEN_PREFIXES},
        _SOFTEN_PREFIXES,
    ):
        for raw, pretty in mapping.items():
            if raw.lower() in title.lower():
                return pretty
    return title + " (приуменьшено докладчиком)"
