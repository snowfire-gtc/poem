"""Ruler's turn report — the chronicle as literature (§13 step 22).

After every server-side turn calculation the player receives a report about
the elapsed interval *from the point of view of the ruler of their
civilization*.  Raw simulation numbers are wrapped into artistic/literary
pieces: verses for era changes and discoveries, epics and laments for wars,
chronicle prose for lawcraft, songs for folklore, warnings for unrest.

Design rules honoured here:

* Determinism first (design.md §4.1): every phrase is chosen by hashing
  stable identifiers (war id, law title, prejudice id…) and by game-time
  values only.  The same finalized turn always renders the byte-identical
  report; wall-clock time never enters (§20.4).
* Game time primary (§20.9): spans, fatigue and tradition are narrated in
  years/seasons, never in "turn counts".
* Reports are generated server-side per civilization and shipped inside the
  published turn report; the thin client displays them verbatim (principle
  1 of ui-client.md).
* Optional LLM prose layer (poem.llm): when an LLM provider is configured,
  template texts become *fallbacks* — each section's raw facts are handed
  to the provider, which returns a fresh literary rewrite.  Prompts derive
  from stable game data only, answers are cached by prompt hash, and any
  provider failure degrades back to the deterministic template, so the
  disabled path stays byte-identical to earlier revisions.

The output structure::

    {
      "civilization_id": "c1",
      "title": "Сказание о ходе 3 · 850 год · Средневековье",
      "verse": ["…", "…"],                 # short programmatic poem
      "sections": [                        # ordered narrative chapters
        {"kind": "wars" | "breakthroughs" | "discoveries" | "unrest"
                 | "laws" | "folklore" | "peace",
         "genre": "epic" | "lyric" | "prose" | "lament" | "warning",
         "title": "…",
         "text": "…",
         "refs": {"war_id": …, "law_id": …}}
      ],
    }
"""

from __future__ import annotations

import hashlib

# ---------------------------------------------------------------------------
# Deterministic selection helpers
# ---------------------------------------------------------------------------


def _h(*parts: object) -> int:
    """Stable non-cryptographic choice index over stringified parts."""
    key = "\x00".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big")


def pick(seq, *salt: object):
    """Deterministically choose one element of ``seq``."""
    return seq[_h(*salt) % len(seq)]


SEASON_LABELS_RU = {
    "spring": "весна", "summer": "лето", "autumn": "осень",
    "winter": "зима", "long_night": "долгая ночь",
}

ERA_GENITIVE_RU = {
    "dawn": "Зари", "ancient": "Древней", "classical": "Классической",
    "medieval": "Средневековья", "renaissance": "Возрождения",
    "industrial": "Индустриальной", "modern": "Современной",
    "atomic": "Атомной", "information": "Информационной",
    "planetary": "Планетарной",
}


def _era_gen(era_value: str) -> str:
    return ERA_GENITIVE_RU.get(era_value, era_value)


def _fmt_year(year: float) -> str:
    return f"{year:g}"


# ---------------------------------------------------------------------------
# Genre templates (all choices keyed by ids + game year only)
# ---------------------------------------------------------------------------

_WAR_EPICS = (
    "Барабаны гнали наши полки через {place}; копья тупились о щиты {enemy}, "
    "но знамя не упало ни разу за {years} лет.",
    "Дым осадных костров стелился над равниной; мы считали ночи по пеплу "
    "сожжённых ворот — {years} лет сталь говорила вместо послов.",
    "Реки носили бронзу и имена павших; в песнях наших {enemy} стал не врагом, "
    "а учителем стойкости — таково право длинной войны.",
    "Князья делили карту, а крестьяне делили одну борону на троих; "
    "{years} лет марш заменял нам календарь.",
)

_WAR_LAMENTS = (
    "Мы хоронили сыновей быстрее, чем успевали давать им имена; "
    "вдовьи песни теперь знают эту границу лучше писцов.",
    "Усталость въелась в стены: мельницы стоят, поля зарастают, "
    "и даже дети поют не о победах, а о возвращении отцов.",
    "Третий набор за {years} лет опустил деревни; народ ропщет шёпотом — "
    "так громче.",
)

_PEACE_VERSES = (
    "Меч остудили в реке, что текла между нами {years} лет;\n"
    "теперь на той же реке — мельницы, а не плоты с копьями.",
    "Послы связали слова там, где мы связывали врагов;\n"
    "мир держится на чернилах — пусть будет крепче стали.",
    "Мы подписали тишину сроком на {years} зим и лет;\n"
    "пусть внуки нашают ей новую прочность, когда лопнет нить.",
    "Послы {enemy} склонились над скатертью, и знамёна убрали в чехлы;\n"
    "{civ} забирает с поля убитых, но не забывает павших.",
)

_RESEARCH_VERSES = (
    "Ночью в подвалах академий догорали свечи — и небо над {civ}\n"
    "стало на одну тайну ближе к нам. За {years} лет — целая бездна света.",
    "Мастера спорили о природе искры и ветра; поспорив — построили.\n"
    "То, что вчера было молнией, сегодня служит кузнецу.",
    "Мы переписали границы возможного на языке чисел;\n"
    "книга эта тонка, но тяжелее всякой казны.",
)

_ERA_CHANGE_VERSES = (
    "Граница эпох прошла не по карте, а по нашим рукам:\n"
    "инструменты прежних лет стали реликвиями в витринах.",
    "Когда-нибудь летописцы назовут эти {years} лет переломом;\n"
    "мы же просто проснулись в мире, который уже умел иначе.",
    "Старое слово «невозможно» вышло из моды вместе с прошлой эпохой.\n"
    "Новая — {era} — говорит своими словами; учимся.",
)

_TRAVEL_PROSE = (
    "Разведчики вернулись с южных берегов с зарисовками чужих башен; "
    "карта пограничья пополчилась на {years}-летнее плавание.",
    "Гонцы принесли соль, шёлк и сведения: земли за перевалом живут по своим "
    "законам — и тоже считают себя центром мира.",
    "Посол {civ} пересёк море и вернулся с договором и со звездной картой "
    "чужих широт.",
)

_UNREST_WARNINGS = (
    "На рынках толпятся и замолкают при моём появлении — так не должно быть. "
    "Голод или обида уже ищут вожака; слушайте город внимательнее.",
    "Народ пересказывает друг другу старые обиды как новости. Пока это "
    "шёпот; завтра он станет криком, если я не отвечу делом.",
    "Гильдии прячут прибыль, деревни не выходят на повинность. Мягкий бунт "
    "тише кованого — и опаснее вдвойне.",
)

_FOLK_SONGS = (
    "«{item}» — эту песню поют уже на всех ярмарках; чем дальше от столицы, "
    "тем больше в ней моих выдуманных родственников.",
    "Сказители распевают «{item}»; каждый добавляет свой куплет, и предание "
    "дрожжит, как вода: узнаваемо — но уже не то.",
    "Про «{item}» сложили припев, которым дети дразнят солдат; фольклор "
    "переваривает мою политику быстрее канцелярии.",
)

_CANCELLED_MOTIF = (
    "«{item}» — сказание, которое народ отверг: его не поют, его помнят "
    "косыми взглядами."
)

_LAW_AMEND_PROSE = (
    "Я повелел переписать «{law}». Писцы спорили о каждой букве {years} лет — "
    "столько стоит приказ, противящийся обычаю.",
    "«{law}» получил новые очертания: там, где прежде был произвол, теперь "
    "прописана мера. Народ привыкнет раньше, чем признает.",
)

_LAW_REPEAL_PROSE = (
    "«{law}» отменён. Закон умер на бумаге, но будет жить в обычаях ещё "
    "поколение — память народа не подписывает указов.",
    "Я снял с народа «{law}». Улицы сначала молчали, потом заговорили — "
    "значит, не зря.",
)

_LAW_SUSPEND_PROSE = (
    "Действие «{law}» заморожено переходным периодом: формально закон жив, "
    "фактически спит. Такие сны иногда кончаются пробуждением похуже."
)

_TRADITION_PROSE = (
    "«{law}» крепит годами: {age} лет на книгах, столько же — в головах. "
    "Теперь тронуть его — всё равно что подвинуть межу общего дома."
)

_ECONOMY_PROSE = (
    "За {years} лет амбары наполнились на {prod:g} меры; народ прибавился "
    "на {pop:g} душ — лучшие стихи этой главы считаются зерном и детьми."
)

_DEFAULT_VERSE = (
    "Тишина была бы скудной главой, но и в тишине варились ремесла,\n"
    "росли дети и старые законы учились новым словам."
)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _section(kind, genre, title, text, refs=None):
    return {"kind": kind, "genre": genre, "title": title,
            "text": text, "refs": refs or {}}


def _build_sections(view: dict, civ_id: str, report: dict,
                    llm=None) -> list[dict]:
    """Compose the narrative chapters for one ruler.

    ``llm`` — optional :class:`poem.llm.LlmTextProvider`.  When given and
    enabled, each template text is offered to it as a deterministic fallback
    alongside the section's raw facts; fresh LLM prose replaces the template,
    failures keep it (§ poem.llm docstring).
    """
    sections: list[dict] = []
    span_years = report.get("years_elapsed", 0)
    era_after = report.get("era_after", "")
    if llm is None:
        try:
            from .llm import get_default_provider
            candidate = get_default_provider()
            llm = candidate if candidate.enabled else None
        except Exception:
            llm = None

    def weave(kind: str, genre: str, title: str, fallback: str,
              refs: dict, facts: dict) -> dict:
        """One section: template text unless the LLM rewrites it."""
        text = fallback
        if llm is not None:
            text = llm.generate(genre, kind, facts,
                                civ_label=civ_label, fallback=fallback)
        return _section(kind, genre, title, text, refs)

    civ_name = None
    for e in view.get("economy", []):
        if e.get("civilization_id") == civ_id:
            civ_name = e.get("name") or civ_id
            break
    civ_label = civ_name or civ_id

    def mine(refs: dict) -> bool:
        cid = refs.get("civilization_id")
        return cid in (None, civ_id)

    names = report.get("_civ_names", {})
    war_parties = report.get("_war_parties", {})

    def enemy_of(refs_cid: str | None, parties: dict | None) -> str:
        """The opposing ruler's name — the report speaks from MY throne."""
        if parties:
            other = (parties.get("defender_id") if parties.get("attacker_id")
                     == refs_cid else parties.get("attacker_id"))
            if other:
                return names.get(other, other)
        return "соседи"

    # --- wars: epic while active, lament when exhaustion bites --------------
    for w in view.get("wars", []):
        wid = w.get("war_id")
        parties = war_parties.get(wid) or {}
        involved = [cid for cid in (parties.get("attacker_id"),
                                    parties.get("defender_id")) if cid]
        if civ_id not in involved:
            continue
        active = bool(w.get("active"))
        fat_key = ("fatigue_attacker"
                   if parties.get("attacker_id") == civ_id
                   else "fatigue_defender")
        fat = float(w.get(fat_key, 0.0))
        foe = enemy_of(civ_id, parties)
        refs = {"war_id": wid, "civilization_id": civ_id}
        if active and fat >= 0.5:
            fallback = pick(_WAR_LAMENTS, wid, "lament").format(
                years=_fmt_year(span_years), enemy=foe, place=foe,
                civ=civ_label)
            sections.append(weave(
                "wars", "lament", "Плач по изнурённой земле", fallback, refs,
                {"war_id": wid, "years_elapsed": _fmt_year(span_years),
                 "fatigue": round(fat, 2), "enemy": foe, "civ": civ_label}))
        elif active:
            # Prefer an epic that actually names the foe; if none of the
            # templates for this war contains {enemy}, force one that does
            # (the report speaks from MY throne and must name the enemy).
            idx = _h(wid, era_after) % len(_WAR_EPICS)
            if "{enemy}" not in _WAR_EPICS[idx]:
                idx = next(i for i, t in enumerate(_WAR_EPICS)
                           if "{enemy}" in t)
            fallback = _WAR_EPICS[idx].format(
                years=_fmt_year(span_years), enemy=foe, place=foe,
                civ=civ_label)
            sections.append(weave(
                "wars", "epic", "Повесть о войне", fallback, refs,
                {"war_id": wid, "years_elapsed": _fmt_year(span_years),
                 "fatigue": round(fat, 2), "enemy": foe, "civ": civ_label}))

    # --- peace treaties concluded this turn (ended wars) --------------------
    for t in view.get("diplomacy", []):
        if t.get("kind") != "peace":
            continue
        if t.get("civilization_id") not in (None, civ_id):
            continue
        payload_refs = {"treaty_id": t.get("treaty_id")}
        ended = t.get("ended_war_id")
        parties = war_parties.get(ended) or {}
        foe = enemy_of(t.get("civilization_id"), parties)
        if ended:
            payload_refs["war_id"] = ended
            fallback = pick(_PEACE_VERSES, ended, era_after).format(
                years=_fmt_year(t.get("remaining_years", 0)),
                enemy=foe, place=foe, civ=civ_label)
            sections.append(weave(
                "peace", "lyric", "Слово о мире", fallback, payload_refs,
                {"war_id": ended, "treaty_id": t.get("treaty_id"),
                 "peace_years": _fmt_year(t.get("remaining_years", 0)),
                 "enemy": foe, "civ": civ_label}))

    # --- scientific & era breakthroughs -------------------------------------
    for e in view.get("economy", []):
        if e.get("civilization_id") != civ_id:
            continue
        research = float(e.get("research", 0.0))
        if research > 0:
            fallback = pick(_RESEARCH_VERSES, civ_id, "research",
                            era_after).format(years=_fmt_year(span_years),
                                              civ=civ_label)
            sections.append(weave(
                "breakthroughs", "lyric", "О науке и ремесле", fallback,
                {"civilization_id": civ_id},
                {"research_points": round(research, 2),
                 "years_elapsed": _fmt_year(span_years),
                 "era": era_after, "civ": civ_label}))
            break

    # economy-as-literature (granary ledger rendered as chronicle prose)
    for e in view.get("economy", []):
        if e.get("civilization_id") != civ_id:
            continue
        prod = float(e.get("production", 0.0))
        growth = float(e.get("population_growth", 0.0))
        if prod > 0 or growth > 0:
            fallback = _ECONOMY_PROSE.format(years=_fmt_year(span_years),
                                             prod=prod, pop=growth)
            sections.append(weave(
                "breakthroughs", "prose", "Летопись амбаров", fallback,
                {"civilization_id": civ_id},
                {"years_elapsed": _fmt_year(span_years), "production": prod,
                 "population_growth": growth, "civ": civ_label}))
        break

    if report.get("era_before") != report.get("era_after"):
        fallback = pick(_ERA_CHANGE_VERSES, era_after, int(report.get(
            "world_year_after", 0))).format(
            years=_fmt_year(span_years), era=_era_gen(era_after).lower())
        sections.append(weave(
            "breakthroughs", "lyric", "Граница эпох", fallback, {},
            {"era_before": report.get("era_before"), "era_after": era_after,
             "years_elapsed": _fmt_year(span_years),
             "world_year": _fmt_year(report.get("world_year_after", 0)),
             "civ": civ_label}))

    # --- geographical discoveries (envoy travels & arrivals) ----------------
    for ev in view.get("events", []):
        if ev.get("kind") == "TravelArrival":
            fallback = pick(_TRAVEL_PROSE, ev.get("detail", ""),
                            ev.get("year", 0)).format(
                years=_fmt_year(span_years), civ=civ_label)
            sections.append(weave(
                "discoveries", "prose", "Чужие земли", fallback,
                {"event_kind": ev.get("kind")},
                {"detail": ev.get("detail", ""),
                 "year": _fmt_year(ev.get("year", 0)),
                 "years_elapsed": _fmt_year(span_years), "civ": civ_label}))

    # --- folk unrest: prejudices & high-mutation lore -----------------------
    for p in view.get("prejudices", []):
        if not mine(p):
            continue
        intensity = float(p.get("intensity", 0.0))
        resolved = bool(p.get("resolved"))
        if not resolved and intensity >= 0.3:
            fallback = pick(_UNREST_WARNINGS, p.get("prejudice_id"),
                            int(intensity * 10))
            sections.append(weave(
                "unrest", "warning", "Тревога в городе", fallback,
                {"prejudice_id": p.get("prejudice_id")},
                {"prejudice_id": p.get("prejudice_id"),
                 "intensity": round(intensity, 2),
                 "topic": p.get("topic", ""), "civ": civ_label}))

    # --- folklore songs ------------------------------------------------------
    lore = {f["folklore_id"]: f for f in view.get("folklore", [])}
    for item in getattr(report, "_folklore_items", []) or []:
        pass  # titles come from the engine handoff below (report metadata)

    for meta in report.get("_folklore_meta", []):
        fid = meta.get("folklore_id")
        m = lore.get(fid)
        if m is None or meta.get("civilization_id") not in (None, civ_id):
            continue
        mutation = float(m.get("mutation", 0.0))
        spread = float(m.get("spread", 0.0))
        title = meta.get("title", fid)
        if bool(meta.get("cancelled")):
            fallback = _CANCELLED_MOTIF.format(item=title)
            sections.append(weave(
                "folklore", "prose", "Отвергнутое сказание", fallback,
                {"folklore_id": fid},
                {"folklore_id": fid, "title": title, "cancelled": True,
                 "mutation": round(mutation, 2), "spread": round(spread, 2),
                 "civ": civ_label}))
        elif spread >= 0.5 or mutation >= 0.5:
            fallback = pick(_FOLK_SONGS, fid, title).format(item=title)
            sections.append(weave(
                "folklore", "lyric", "Голос улиц", fallback,
                {"folklore_id": fid},
                {"folklore_id": fid, "title": title,
                 "mutation": round(mutation, 2), "spread": round(spread, 2),
                 "civ": civ_label}))

    # --- lawcraft ------------------------------------------------------------
    applied = set(report.get("applied_revisions", []))
    laws_by_id = {lw.get("id"): lw for lw in view.get("laws", [])}
    for rev in report.get("_revisions_meta", []):
        rid = rev.get("revision_id")
        if rid not in applied:
            continue
        cid = rev.get("civilization_id")
        if cid not in (None, civ_id):
            continue
        law = laws_by_id.get(rev.get("law_id"), {})
        title = law.get("title") or rev.get("law_id") or rid
        action = rev.get("action")
        refs = {"law_id": rev.get("law_id"), "revision_id": rid,
                "civilization_id": cid}
        if action == "RepealLaw":
            fallback = pick(_LAW_REPEAL_PROSE, title, rid).format(law=title)
        elif action == "SuspendLaw":
            fallback = _LAW_SUSPEND_PROSE.format(law=title)
        else:
            fallback = pick(_LAW_AMEND_PROSE, title, rid).format(
                years=_fmt_year(span_years), law=title)
        law_facts = {"law_id": rev.get("law_id"), "revision_id": rid,
                     "law_title": title, "action": action,
                     "years_elapsed": _fmt_year(span_years),
                     "civ": civ_label}
        sections.append(weave("laws", "prose", "Указы и чернила",
                              fallback, refs, law_facts))
        # entrenched laws read as heritage, not decrees
        tradition = float(law.get("tradition", 0.0))
        if tradition >= 0.5:
            fallback = _TRADITION_PROSE.format(
                law=title, age=_fmt_year(law.get("age_years", 0)))
            sections.append(weave(
                "laws", "prose", "Корни закона", fallback, refs,
                {"law_id": rev.get("law_id"), "law_title": title,
                 "tradition": round(tradition, 2),
                 "age_years": _fmt_year(law.get("age_years", 0)),
                 "civ": civ_label}))

    return sections


def _title_of(turn_number, current_year, era_value) -> str:
    return (f"Сказание о ходе {turn_number} · "
            f"{_fmt_year(current_year)} год · {_era_gen(era_value)} век")


def build_ruler_report(view: dict, report: dict, civilizations: list,
                       folklore_meta: list, llm=None) -> list[dict]:
    """Compose one literary report per civilization (§13 step 22).

    ``view``  — the published client view (dashboard/economy/wars/…),
    ``report``— raw published turn report (applied revisions, era bounds),
    ``civilizations`` — [{id,name,…}] registered in the world,
    ``folklore_meta`` — [{folklore_id,civilization_id,title,canceled}],
    ``llm``   — optional :class:`poem.llm.LlmTextProvider`; when omitted the
                process-wide provider is consulted (no-op unless LLM mode is
                enabled via POEM_LLM_ENABLED).
    Pure function of these inputs: deterministic and time-of-day free; with
    an LLM provider the texts are model-written but still reproducible
    through prompt-hash caching (see poem/llm.py).
    """
    d = view.get("dashboard", {})
    out = []
    enriched = {**view}
    merged_report = {**report,
                     "_folklore_meta": folklore_meta,
                     "_revisions_meta": report.get("_revisions_meta", [])}
    for civ in civilizations:
        civ_id = civ.get("id") if isinstance(civ, dict) else getattr(civ, "id", None)
        if civ_id is None:
            continue
        sections = _build_sections(enriched, civ_id, merged_report, llm=llm)
        if not sections:
            quiet_fallback = _DEFAULT_VERSE
            quiet_refs = {"civilization_id": civ_id}
            quiet_text = quiet_fallback
            if llm is not None:
                quiet_text = llm.generate(
                    "lyric", "folklore",
                    {"quiet_interval": True, "turn": d.get("turn", 0),
                     "world_year": _fmt_year(d.get("current_year", 0)),
                     "era": d.get("era_value", ""),
                     "civ": civ_id},
                    civ_label=civ_id, fallback=quiet_fallback)
            sections = [_section("folklore", "lyric", "Тихий интервал",
                                 quiet_text, quiet_refs)]
        verse_lines = [s["text"] for s in sections if s["genre"] == "lyric"]
        hero = verse_lines[0] if verse_lines else _DEFAULT_VERSE
        out.append({
            "civilization_id": civ_id,
            "title": _title_of(d.get("turn", 0), d.get("current_year", 0),
                               d.get("era_value", "")),
            "verse": [ln.strip() for ln in hero.splitlines()],
            "sections": sections,
        })
    return out
