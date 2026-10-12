"""Карта территорий через призму знаний цивилизации игрока (ui-client.md §2.11).

Экран «🗺 Карта» (#/map) показывает NOT сырое состояние мира, а проекцию
территорий так, как их видит выбранная цивилизация — по тем же законам
детерминизма и тонкого клиента, что и остальные представления (§13 шаг 19):

 * **Призма знания.** Сеттлемент виден игроку только в той мере, в какой о
   нём знает его правовая и языковая система: освоенные земли (свои
   поселения) показаны подробно; соседние — ровно на уровне взаимной
   понятности речи (``mutual_intelligibility``), потому что «далёкую землю
   описывают те слова, которые до неё дошли»; неведомые — как «terra
   incognita». Ни один скрытый параметр чужой цивилизации (лояльность элит,
   броски Фортуны, точный состав войск соперника) сюда не попадает (§22.8).

 * **Игровое время первично.** Степень изученности («глубина знания») —
   функция игрового года, населения, дорог и единства языка; wall-clock не
   используется нигде (§20.4), поэтому повторный запрос в тот же ход даёт
   байт-идентичный ответ (§4.1).

 * **Искажение летописи.** При включённой политике (Rev 4) нарративные
   подписи карты смягчаются лестью двора в той же мере, что и хроника
   (§22.5): чем тираннее правитель и сильнее самообман, тем розовее
   «картинка окраин». Структурные числа при этом остаются каноническими,
   а payload несёт метку ``narrative: true`` / ``canonical: true``.

Модуль ничего не хранит: он вычисляет представление из состояния сервера
(поселения + диалекты, законы, отчёт хода, цивилизации) по запросу клиента.
"""

from __future__ import annotations

import hashlib
import math

#: Что клиент умеет рисовать для каждого типа местности (ui-client.md §2.11).
TERRAIN_GLYPHS: dict[str, str] = {
    "plain": "﹏",       # волна равнины
    "hills": "︩",
    "mountain": "▲",
    "desert": "∴",
    "marsh": "⁙",
    "island": "◍",
}

TERRAIN_LABELS_RU: dict[str, str] = {
    "plain": "равнина",
    "hills": "холмы",
    "mountain": "горы",
    "desert": "пустыня",
    "marsh": "болота",
    "island": "остров",
}

#: Основа названия места по его характерной особенности — «народ даёт имя
#: тому, что видит» (лор мест). Первая подходящая черта побеждает.
PLACE_NAME_BASES: tuple[tuple[str, str], ...] = (
    ("mountain", "Кремень-гора"),
    ("marsh", "Мшаное"),
    ("desert", "Полынь"),
    ("island", "Чайкин"),
    ("hills", "Луговое"),
    ("plain", "Ровное"),
)

#: Детерминированные «прикрасы» названий — выбор только по stable-id (§4.1).
PLACE_NAME_SUFFIXES: tuple[str, ...] = (
    "Грядская", "Притопкая", "Кривбродая", "Седая", "Волчья",
    "Соленая", "Дымчатая", "Ясеневая", "Каменица", "Берегиня",
)

#: Эпитеты к имени места — тоже от его характера (лор), выбор по id.
PLACE_NAME_EPITHETS: dict[str, tuple[str, ...]] = {
    "mountain": ["крутая", "ветреная", "несступная"],
    "hills": ["пологая", "перелесная", "опольная"],
    "plain": ["раздольная", "хлебная", "пыльная"],
    "desert": ["жадная", "знойная", "молчаливая"],
    "marsh": ["глухая", "комариная", "топкая"],
    "island": ["обманчивая", "маячная", "чайкина"],
    "unknown": ["неведомая"],
}

#: Готовые топонимы чужих/далёких земель — как их назвал бы народ, не бывавший
#: там, но слышавший слова (уровень «смутно»).
DIM_PLACE_NAMES: tuple[str, ...] = (
    "земля у реки", "заброда", "полуденный край", "западный рубеж",
    "северская глушь", "восточная сторона",
)

#: Мифы и предания о далёких землях — чем дальше и неизвестнее, тем страшнее
#: («за пределом слова живут представления народа, а не земли»).
MYTH_POOL: tuple[str, ...] = (
    "за краем карты шумит Великий Океан, а на дне его спят города предков",
    "там солнце садится в воду и утром выходит из неё обожжённым",
    "живут одноглазые всадники: кто взглянет им в глаза — сам станет тенью",
    "реки текут вверх, и по ним плавают лодки с костями мудрецов",
    "гора, что ходит: раз в сто лет переступает с места на место",
    "люди с собачьими головами пишут законы когтем на бересте — и слово их крепче нашего",
    "лес, где деревья помнят имена заблудившихся и шепчут их ночью",
    "страна вечной весны: её берегут драконы, спящие под порогами",
    "край мира, где небо опирается на слонов; подуй сильнее — и всё посыплется",
    "дымящиеся топи: болотные огни манят путников и ведут их к золоту — вниз",
)

#: Предвестия и приметы для слоя «слухи» (уровень «смутно»).
RUMOR_POOL: tuple[str, ...] = (
    "купцы бают: за рубежом — страна, где правит Железный Царь",
    "сказители спорят: кончается ли там земля или только слово",
    "беглецы клянутся, что видели корабли выше наших церквей",
    "на ночном небе созвездие Путешественника указывает путь туда",
    "поговаривают: там рождён наш койне — и позабыт",
)

#: Уровень познания территории, который клиент превращает в стиль рисования.
KNOWING_LEVELS = ("known", "dim", "unknown")

#: Поддерживаемые картографические проекции (выбор клиента, server считает
#: координаты тайлов; детерминизм: одна проекция — один байт-ответ).
PROJECTIONS = ("equirectangular", "mercator", "conic")


def _rng(*parts) -> float:
    """Детерминированный псевдослучай number в [0,1) из stable id (без RNG-stream).

    Детерминизм важнее качества перемешивания: один и тот же id всегда даёт
    одну и ту же «погоду» на карте (§4.1).
    """
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def knowing_depth(settlement, civ, dialect, koine_divergence: float,
                  laws_of_civ: list, year: float) -> float:
    """Глубина знания своей территории, 0..1 — «сколько земли знает право».

    Компоненты (все — игровые величины, никаких реальных часов):
      * оседлость и людность (логарифмически: 1000 душ ≈ 0.35, 1e6 ≈ 1.0);
      * дороги/гавани (infrastructure) — знание возит колесо;
      * единая речь (unity) — указ, прочитанный на местном наречии, знает
        больше, чем тот, которого не могут прочитать;
      * письменность/надзор: активные законы про «перепись», «налог»,
        «дозор», «дороги» добавляют измеренности земле.
    """
    pop = max(1.0, float(getattr(settlement, "pop", 0.0) or 0.0))
    settlement_weight = min(1.0, math.log10(pop) / 6.0)          # log10(1e6)=6
    infra = min(1.0, max(0.0, float(getattr(settlement, "infrastructure", 0.0))))
    unity = min(1.0, max(0.0, float(getattr(civ, "language_unity", 1.0))))
    divergence = min(1.0, max(0.0, koine_divergence))

    written = 0.0
    for law in laws_of_civ:
        if getattr(law, "status", None) is not None and \
                str(getattr(law.status, "value", law.status)) != "active":
            continue
        text = f"{getattr(law, 'title', '')} {getattr(law, 'raw_text', '')}".lower()
        canon = getattr(law, "canonical", None)
        kind = str(getattr(canon, "kind", "") or "").lower() if canon else ""
        hay = text + " " + kind
        if any(w in hay for w in ("перепис", "учёт", "налог", "дозор", "опись",
                                  "дорог", "мост", "гаван", "порт", "письмен")):
            written += 0.08 + 0.05 * min(1.0, float(getattr(law, "tradition", 0.0)))
    written = min(0.45, written)

    depth = (0.40 * settlement_weight + 0.22 * infra + 0.18 * unity
             + 0.10 * (1.0 - divergence) + 0.10 * min(1.0, written / 0.45))
    # Длительное существование поселения добавляет уверенности летописи.
    age_years = max(0.0, year - float(getattr(dialect, "created_year", year) or year))
    depth *= 1.0 + min(0.08, 0.0002 * age_years)
    return round(min(1.0, max(0.05, depth)), 4)


def foreign_visibility(distance_band: int, intelligibility: float,
                       has_envoy: bool, has_treaty: bool, war: bool) -> float:
    """Видимость чужой земли: расстояние × понятность речи × дипломатия.

    ``distance_band`` — условная удалённость 0..4 (0 — общий рубеж). Посольство
    и договор расширяют познание, война делает соседа заметным, но искажённым.
    """
    base = max(0.0, 1.0 - 0.24 * distance_band)
    speech = 0.35 + 0.65 * intelligibility
    v = base * speech
    if has_envoy:
        v += 0.18
    if has_treaty:
        v += 0.14
    if war:
        v = min(1.0, v + 0.22)          # о враге знают много — и мало правды
    return round(min(1.0, max(0.0, v)), 4)


def level_of(depth: float) -> str:
    if depth >= 0.62:
        return "known"
    if depth >= 0.28:
        return "dim"
    return "unknown"


def _soften_label(text: str, bias: float) -> str:
    """Нарративная подпись карты: чем сильнее лесть двора, тем слаще слово."""
    if bias <= 0.0:
        return text
    if bias < 0.35:
        return text + " — как доложили писцы"
    if bias < 0.7:
        return text.replace("глухая", "тихая").replace("дикая", "вольная") + \
               " (в летописи краше)"
    return "благодатная окраина, славная во всём"


def build_map_view(server, civ_id: str | None = None) -> dict:
    """Собрать карту территорий для одной цивилизации (тонкий клиент читает как есть).

    ``server`` — :class:`poem.api.WorldServer`; источник истины: engine.language
    (поселения/диалекты), store (законы), civilizations, последний опубликованный
    отчёт хода (войны/договоры). Ответ содержит ``layers`` (сеттлементы, границы
    познания, маркеры знаний) и ``legend`` для UI.
    """
    lang = server.engine.language
    civ_ids = [c.id for c in server.civilizations.values() if c.active]
    if civ_id:
        if civ_id not in server.civilizations:
            raise ValueError(f"unknown civilization {civ_id!r}")
    else:
        civ_id = civ_ids[0] if civ_ids else (server.civilizations.keys().__next__()
                                             if server.civilizations else None)
    if civ_id is None:
        return {"civilization_id": None, "layers": [], "legend": {},
                "note": "в мире нет цивилизаций — карта пуста"}

    civ = server.civilizations[civ_id]
    year = server.chronology.current_year
    koine = lang.koine(civ_id)
    own_settlements = [s for s in lang.settlements if s.civilization_id == civ_id]
    other_settlements = [s for s in lang.settlements if s.civilization_id != civ_id]
    own_laws = [law for law in server.store._laws.values() if law.civilization_id == civ_id]

    report = (server.published_reports[-1] if server.published_reports else {})
    view = report.get("view") or {}
    wars = view.get("wars") or []
    treaties = view.get("diplomacy") or []

    def parties_of(obj) -> set:
        ids = set()
        for key in ("attacker_id", "defender_id", "party_a", "party_b",
                    "other_party", "civilization_id"):
            val = obj.get(key)
            if isinstance(val, str):
                ids.add(val)
        return ids

    at_war = set().union(*(parties_of(w) for w in wars)) if wars else set()
    treaty_partners = set().union(*(parties_of(t) for t in treaties)) if treaties else set()
    active_wars = [w for w in wars if w.get("active", 1)]

    settlements_layer = []
    knowledge_markers = []

    # --- свои земли: подробное знание -------------------------------------
    for s in sorted(own_settlements, key=lambda x: x.id):
        d = lang.dialect_of(s)
        div = float(getattr(d, "divergence", 0.0)) if d else 0.0
        depth = knowing_depth(s, civ, d, div, own_laws, year)
        settlements_layer.append({
            "id": s.id,
            "name": s.name or s.id,
            "owner": civ_id,
            "relation": "own",
            "terrain": s.terrain.value,
            "glyph": TERRAIN_GLYPHS.get(s.terrain.value, "•"),
            "terrain_label": TERRAIN_LABELS_RU.get(s.terrain.value, s.terrain.value),
            "knowing": level_of(depth),
            "depth": depth,
            "population": round(float(s.pop), 1),
            "infrastructure": round(float(s.infrastructure), 4),
            "dialect": (d.name if d else "койне"),
            "divergence": round(div, 4),
            "label": _own_label(s, d, div, depth),
        })

    # --- чужие земли: ровно столько, сколько дошло слов -------------------
    for s in sorted(other_settlements, key=lambda x: x.id):
        owner = s.civilization_id
        band = _distance_band(s, own_settlements)
        od = lang.dialect_of(s)
        intel = _intelligibility(koine, od)
        vis = foreign_visibility(
            band, intel,
            has_envoy=any(t.get("kind") in ("envoy", "embassy", "Envoy") or
                          owner in parties_of(t) for t in treaties),
            has_treaty=owner in treaty_partners,
            war=owner in at_war and bool(active_wars))
        known = vis >= 0.62
        dim = 0.28 <= vis < 0.62
        settlements_layer.append({
            "id": s.id,
            "name": (s.name or s.id) if known else ("земля у реки" if dim else ""),
            "owner": owner if known else None,
            "relation": "foreign",
            "terrain": s.terrain.value if (known or dim) else "unknown",
            "glyph": TERRAIN_GLYPHS.get(s.terrain.value, "?") if (known or dim) else "?",
            "terrain_label": TERRAIN_LABELS_RU.get(s.terrain.value, "неведомо")
                             if (known or dim) else "неведомая земля",
            "knowing": level_of(vis),
            "depth": vis,
            "population": round(float(s.pop), 1) if known else None,
            "infrastructure": None,
            "dialect": (od.name if (od and known) else None),
            "divergence": round(float(getattr(od, "divergence", 0.0)), 4) if known else None,
            "intelligibility": round(intel, 4),
            "distance_band": band,
            "label": _foreign_label(owner, band, intel, known, dim,
                                   owner in at_war and bool(active_wars)),
        })
        if not known:
            knowledge_markers.append({
                "kind": "terra_incognita" if not dim else "rumor",
                "near": s.id,
                "text": ("ходят слухи о землях за рубежом" if dim
                         else "за пределом слова — terra incognita"),
            })

    # --- маркеры знания: законы-«измерители» и события хроники ------------
    for law in own_laws:
        canon_kind = str(getattr(getattr(law, "canonical", None), "kind", "") or "")
        if canon_kind in ("tax", "census", "survey", "road", "levy") or \
                any(w in (law.title or "").lower() for w in ("перепис", "дорог", "дозор")):
            knowledge_markers.append({
                "kind": "law_measure",
                "law_id": law.id,
                "title": law.title,
                "text": f"закон «{law.title}» измеряет землю: "
                        f"традиция {round(float(law.tradition), 2)}",
            })
    for ev in view.get("events") or []:
        if ev.get("kind") in ("TravelArrival", "TreatyExpired", "EraChange"):
            knowledge_markers.append({
                "kind": "chronicle",
                "year": ev.get("year"),
                "text": f"[{ev.get('year'):g}] {ev.get('title')}"
                        if isinstance(ev.get("year"), (int, float)) else ev.get("title"),
            })

    # --- нарративное искажение подписей (Rev 4 §22.5) ----------------------
    distortion = _map_distortion(server, civ_id, settlements_layer)

    borders = _knowledge_borders(settlements_layer)

    layers = [
        {"id": "relief", "title": "Рельеф известной земли",
         "items": [{"terrain": sl["terrain"], "glyph": sl["glyph"]}
                   for sl in settlements_layer]},
        {"id": "settlements", "title": "Поселения", "items": settlements_layer},
        {"id": "borders", "title": "Рубежи познания", "items": borders},
        {"id": "knowledge", "title": "Знания и слухи", "items": knowledge_markers},
    ]

    return {
        "civilization_id": civ_id,
        "civilization_name": civ.name or civ_id,
        "world_year": year,
        "turn": server.chronology.current_turn,
        "era": server.chronology.current_era.value,
        "layers": layers,
        "legend": {
            "levels": [
                {"level": "known", "label": "изведано", "hint": "земля описана законом и переписью"},
                {"level": "dim", "label": "смутно", "hint": "дошли только слова, не цифры"},
                {"level": "unknown", "label": "неведомо", "hint": "за пределами языка и права"},
            ],
            "terrains": {t: {"glyph": g, "label": TERRAIN_LABELS_RU.get(t, t)}
                         for t, g in TERRAIN_GLYPHS.items()},
        },
        "distortion": distortion,
        "canonical": not distortion.get("applied", False),
        "narrative": bool(distortion.get("applied", False)),
        "note": "Карта построена через призму знаний вашей цивилизации: "
                "соседи видны ровно настолько, насколько до них дошло ваше слово.",
    }


def _own_label(s, d, div: float, depth: float) -> str:
    terrain = TERRAIN_LABELS_RU.get(s.terrain.value, s.terrain.value)
    if depth >= 0.62:
        speech = "говорит на койне" if (d is None or d.parent_id is None) \
            else f"говорит «{d.name}»"
        return f"{s.name or s.id}: {terrain}, людность {round(s.pop)} , {speech}"
    if div >= 0.35:
        return f"{s.name or s.id}: глухая {terrain}, речь её отдалилась от койне"
    return f"{s.name or s.id}: {terrain}, описана лишь наполовину"


def _foreign_label(owner, band: int, intel: float, known: bool, dim: bool,
                   war: bool) -> str:
    if known:
        tone = "враждебный" if war else ("союзный" if band <= 1 else "дальний")
        return f"земли соседа ({owner}): {tone}, слова понятны на {intel:.0%}"
    if dim:
        return "чужая окраина — имя и людность ещё не дошли"
    return "terra incognita"


def _distance_band(s, own_settlements) -> int:
    """Условная удалённость от ближайшего своего поселения, 0..4.

    Настоящей географии (координат) в ядре нет, поэтому расстояние выводится
    детерминированно из stable-id: это витрина проекции знания, а не физика
    мира; одинаковый вход ⇒ одинаковая карта (§4.1).
    """
    if not own_settlements:
        return 4
    r = _rng("band", s.id, *(x.id for x in sorted(own_settlements, key=lambda x: x.id)))
    return min(4, int(r * 5))


def _intelligibility(a, b) -> float:
    from .core import mutual_intelligibility
    return mutual_intelligibility(a, b)


def _knowledge_borders(rows: list) -> list:
    """Линии рубежей: где «изведано» переходит в «смутно», а там — в пустоту."""
    out = []
    order = {"known": 0, "dim": 1, "unknown": 2}
    prev = None
    for row in sorted(rows, key=lambda r: (-order.get(r["knowing"], 2), r["id"])):
        lvl = row["knowing"]
        if prev is not None and prev != lvl:
            out.append({"from_level": prev, "to_level": lvl,
                        "anchor": row["id"],
                        "text": f"рубеж познания: {prev} → {lvl}"})
        prev = lvl
    return out


def _map_distortion(server, civ_id: str, rows: list) -> dict:
    """Leest/drop adverse map labels when politics is on (§22.5 semantics).

    Structured numbers stay canonical; only human-readable labels soften.
    Returns an audit dict so the client can print the «narrative» badge.
    """
    politics = getattr(server.engine, "politics", None)
    enabled = bool(politics is not None and getattr(politics, "enabled", False))
    if not enabled:
        return {"applied": False, "fields": [], "bias": 0.0}
    civ = server.civilizations.get(civ_id)
    ruler = getattr(civ, "ruler", None) if civ else None
    if ruler is None:
        return {"applied": False, "fields": [], "bias": 0.0}
    grievance = 0.0
    try:
        factions = server.engine.elites_factions_for(civ_id) if hasattr(
            server.engine, "elites_factions_for") else []
    except Exception:
        factions = []
    if factions:
        grievance = sum(float(getattr(f, "grievance", 0.0)) for f in factions) / len(factions)
    from .distortion import flattery_bias
    bias = flattery_bias(ruler.style, grievance, politics.report.flattery_scale)
    bias = max(0.0, bias + 0.5 * ruler.self_deception)
    touched = []
    if bias > 0.0:
        for i, row in enumerate(rows):
            if row.get("relation") != "own":
                continue
            label = row.get("label", "")
            soft = _soften_label(label, bias)
            if soft != label:
                row["label"] = soft
                touched.append(f"layers[settlements].items[{i}].label")
    return {"applied": bool(touched), "fields": touched, "bias": round(bias, 4)}


# ============================================================================
#  Карта: тайлы, топонимы-лор, Terra Incognita с мифами, бюджет посещений
# ============================================================================

#: Базовая стоимость перехода в полосу удалённости (игровые «дни пути»).
BAND_TRAVEL_COST = (0.25, 0.5, 1.0, 2.0, 4.0)
#: Terrain-поправщик к цене пути: по болотам и горам идут медленнее (§ лора).
TERRAIN_TRAVEL_MULT = {
    "plain": 1.0, "hills": 1.15, "mountain": 1.6,
    "desert": 1.4, "marsh": 1.5, "island": 1.25, "unknown": 1.8,
}


def _stable_int(*parts) -> int:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(h[:12], 16)


def place_name(settlement_id: str, terrain: str, given_name: str = "") -> dict:
    """Имя места глазами народа: свои поселения носят данное имя, неведомые
    земли народ называет сам — по их характерной особенности (лор мест).

    Выбор эпитета/прикрасы детерминирован из stable-id (§4.1): одна и та же
    карта при повторном запросе байт-идентична.
    """
    base = None
    for t, b in PLACE_NAME_BASES:
        if terrain == t:
            base = b
            break
    epithets = PLACE_NAME_EPITHETS.get(terrain, PLACE_NAME_EPITHETS["unknown"])
    epi = epithets[_stable_int("epi", settlement_id) % len(epithets)]
    suffix = PLACE_NAME_SUFFIXES[_stable_int("suf", settlement_id) % len(PLACE_NAME_SUFFIXES)]
    if given_name:
        return {"name": given_name, "lore_name": f"{given_name} {epi}",
           "origin": "дано поселению людьми"}
    return {"name": f"{base} {suffix}", "lore_name": f"{base} {suffix}, {epi}",
            "origin": f"название по особенности места: {TERRAIN_LABELS_RU.get(terrain, terrain)}"}


def project_point(lon: float, lat: float, projection: str) -> tuple[float, float]:
    """Спроецировать точку (lon -180..180, lat -85..85) в плоскость карты.

    Детерминированная математика без wall-clock: клиент выбирает проекцию,
    сервер считает координаты тайлов; одинаковый вход ⇒ одинаковый ответ.
    """
    phi = math.radians(max(-85.0, min(85.0, lat)))
    theta = math.radians(lon)
    if projection == "mercator":
        y = math.log(math.tan(math.pi / 4 + phi / 2))
        return round(theta, 6), round(y, 6)
    if projection == "conic":
        k = math.cos(math.radians(38.0))          # конформный конус средн. широт
        n = math.sin(phi) if abs(phi) > 1e-9 else 1e-9
        rho = k / n
        return round(rho * math.sin(k * theta), 6), round(rho * math.cos(k * theta), 6)
    return round(theta, 6), round(phi, 6)         # equirectangular (default)


def build_map_tiles(server, civ_id: str, settlements_layer: list,
                    projection: str = "equirectangular") -> list[dict]:
    """Тайловая сетка известной земли: свои — раскрашены по глубине знания,
    чужие смутные — полутон, неведомые — пустые клетки с мифом.

    Координаты тайлов выводятся детерминированно из stable-id (в ядре нет
    настоящей географии — это витрина проекции знания, а не физика мира).
    """
    grid_w, grid_h = 12, 8
    cells: dict[tuple[int, int], dict] = {}
    own = [r for r in settlements_layer if r.get("relation") == "own"]
    for row in sorted(settlements_layer, key=lambda r: r["id"]):
        idx = _stable_int("tile", civ_id, row["id"])
        gx = idx % grid_w
        gy = (idx // grid_w) % grid_h
        lon = -180.0 + (gx + 0.5) * (360.0 / grid_w)
        lat = 70.0 - (gy + 0.5) * (140.0 / grid_h)
        px, py = project_point(lon, lat, projection)
        knowing = row.get("knowing", "unknown")
        tile = {
            "settlement_id": row["id"],
            "grid": [gx, gy],
            "xy": [px, py],
            "knowing": knowing,
            "glyph": row.get("glyph", "?"),
            "terrain": row.get("terrain", "unknown"),
            "place_name": row.get("name") or "",
            "depth": row.get("depth", 0.0),
        }
        if knowing == "unknown":
            tile["myth"] = myth_for(civ_id, gx, gy, own)
        elif row.get("relation") == "own":
            # изведанная своя земля: подпись народа — имя + эпитет по характеру места
            lore = place_name(row["id"], tile["terrain"], row.get("name") or "")
            tile["lore_name"] = lore["lore_name"]
            tile["name_origin"] = lore["origin"]
        cells[(gx, gy)] = tile
    return [cells[k] for k in sorted(cells)]


def myth_for(civ_id: str, gx: int, gy: int, own_rows: list) -> str:
    """Миф/предание о далёкой земле для клетки Terra Incognita.

    Выбор — из представлений народа о путешествиях и дальних краях; чем
    дальше от обжитого центра, тем «страшнее» берётся предание (детерминированно).
    """
    spread = max(1, len(MYTH_POOL))
    pick = _stable_int("myth", civ_id, gx, gy, len(own_rows)) % spread
    return MYTH_POOL[pick]


def rumor_for(civ_id: str, anchor: str) -> str:
    """Слух о смутно ведомой земле — тоже из преданий, но мягче мифа."""
    return RUMOR_POOL[_stable_int("rumor", civ_id, anchor) % len(RUMOR_POOL)]


def travel_budget(server, civ_id: str) -> dict:
    """Сколько мест цивилизация может посетить за ход (деньги хода — игровое
    время §20.4): транспортная сеть (infrastructure койне-оседлостей),
    технологический уровень эпохи и единство языка.

    Формула открыта клиенту целиком: карта честно показывает, почему сегодня
    доступно ровно N переходов.
    """
    from .chronology import Era
    from .language import transport_tech_level
    civ = server.civilizations[civ_id]
    era = server.chronology.current_era
    tech = transport_tech_level(civ, era if isinstance(era, Era) else Era[era.upper()]) \
        if era is not None else 0.0
    lang = server.engine.language
    own = [s for s in lang.settlements if s.civilization_id == civ_id]
    roads = sum(min(1.0, max(0.0, float(getattr(s, "infrastructure", 0.0))))
                for s in own) / max(1, len(own)) if own else 0.0
    unity = min(1.0, max(0.0, float(getattr(civ, "language_unity", 1.0))))
    reach = 1.0 + 3.0 * tech + 2.0 * roads + unity          # «сырые мили» хода
    visits = max(1, min(5, int(reach // 2)))
    return {
        "visits_per_turn": visits,
        "transport_tech": round(float(tech), 4),
        "roads_index": round(roads, 4),
        "language_unity": round(unity, 4),
        "explanation": ("мест за ход = 1 + связь×3 + дороги×2 + речь; "
                        "развитие транспортных сетей и технологий открывает дальние земли"),
    }


def visit_options(server, civ_id: str, map_view: dict, tiles: list) -> dict:
    """Куда можно послать экспедицию в этом ходу и чего это стоит.

    Цена — «дни пути» до полосы удалённости с terrain-поправкой; доступные
    только изведанные и смутные земли (в миф идти можно — но карта не ручается).
    """
    budget = travel_budget(server, civ_id)
    rows = []
    for t in tiles:
        sid = t.get("settlement_id")
        row = next((r for r in map_view_layers_settlements(map_view) if r["id"] == sid), None)
        if row is None:
            continue
        band = int(row.get("distance_band", 0)) if row.get("relation") == "foreign" else 0
        cost = BAND_TRAVEL_COST[min(band, 4)] * TERRAIN_TRAVEL_MULT.get(t.get("terrain", "plain"), 1.0)
        cost = round(cost, 3)
        reachable = (row.get("relation") == "own" or t.get("knowing") in ("known", "dim")) \
            and cost <= budget["visits_per_turn"]
        rows.append({
            "settlement_id": sid,
            "name": t.get("place_name") or t.get("lore_name") or row.get("name") or sid,
            "knowing": t.get("knowing"),
            "travel_days": cost,
            "reachable_this_turn": bool(reachable),
            "reason": "" if reachable else (
                "слишком далеко для нынешних дорог" if cost > budget["visits_per_turn"]
                else "за пределом познанного — только по слухам"),
        })
    rows.sort(key=lambda r: (not r["reachable_this_turn"], r["travel_days"], r["settlement_id"]))
    return {"budget": budget, "options": rows}


def map_view_layers_settlements(view: dict) -> list:
    for layer in view.get("layers", []):
        if layer.get("id") == "settlements":
            return layer.get("items", [])
    return []


def _civ_projection_payload(server, civ_id: str, projection: str) -> dict:
    """Сборка карты для ОДНОЙ цивилизации (без try/except — ошибки наружу)."""
    view = build_map_view(server, civ_id)
    if not view.get("civilization_id"):
        return {**view, "projection": projection, "tiles": [],
                "travel": {"visits_per_turn": 0, "explanation": "нет цивилизации"},
                "visits": {"budget": {}, "options": []}}
    cid = view["civilization_id"]
    settlements = map_view_layers_settlements(view)
    tiles = build_map_tiles(server, cid, settlements, projection)
    # Смутные земли: заменяем общие слухи на предания народа (тот же детерминизм).
    for marker in view.get("layers", []):
        if marker.get("id") != "knowledge":
            continue
        for item in marker.get("items", []):
            if item.get("kind") == "terra_incognita":
                item["text"] = "за пределом слова — " + myth_for(
                    cid, _stable_int("mx", cid, item.get("near", "")) % 12,
                    _stable_int("my", cid, item.get("near", "")) % 8, settlements)
            elif item.get("kind") == "rumor":
                item["text"] = rumor_for(cid, item.get("near", ""))
    visits = visit_options(server, cid, view, tiles)
    return {
        **view,
        "projection": projection,
        "projections_available": list(PROJECTIONS),
        "tiles": tiles,
        "travel": visits["budget"],
        "visits": visits,
    }


def build_full_map(server, civ_id: str | None = None,
                   projection: str = "equirectangular") -> dict:
    """Единая сборка карты для клиента: призма знания + тайлы + мифы + путь.

    Возвращает базовый :func:`build_map_view` плюс поля ``projection``,
    ``tiles`` (тайлы с топонимами-лором и мифами Terra Incognita),
    ``travel`` (бюджет посещений за ход) и ``visits`` (опции экспедиций).
    Без явного ``civ_id`` карта строится через призму первой активной
    цивилизации мира; если её состояние не позволяет описать карту,
    перебираются остальные — тонкий клиент всегда получает честную
    пустую карту вместо ошибки (§6 ui-client.md).
    Детерминизм сохраняется: тот же мир, тот же ход, та же проекция —
    байт-идентичный JSON (§4.1).
    """
    if projection not in PROJECTIONS:
        raise ValueError(f"unknown projection {projection!r}; "
                         f"expected one of {list(PROJECTIONS)}")
    if civ_id:
        return _civ_projection_payload(server, civ_id, projection)
    # Без явной цивилизации: первая активная; нечитаемая призма — следующая.
    candidates = [c.id for c in server.civilizations.values() if c.active] \
        or list(server.civilizations.keys())
    last_error: Exception | None = None
    for cid in candidates:
        try:
            return _civ_projection_payload(server, cid, projection)
        except Exception as exc:          # состояние мира не даёт карты — пробуем дальше
            last_error = exc
    if last_error is not None:
        raise last_error
    return {"civilization_id": None, "layers": [], "legend": {},
            "projection": projection, "tiles": [],
            "travel": {"visits_per_turn": 0, "explanation": "нет цивилизации"},
            "visits": {"budget": {}, "options": []},
            "note": "в мире нет цивилизаций — карта пуста"}
