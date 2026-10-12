"""Tests for the «🗺 Карта» screen (ui-client.md §2.11): GET /worlds/{id}/map.

The map is a projection of the world through the prism of the chosen
civilization's knowledge — never the raw server state (§22.8):

 * own lands are described in detail (law + census numbers visible);
 * foreign lands are visible only as far as speech reached them
   (mutual intelligibility, distance band, diplomacy);
 * unknown lands come back as Terra Incognita carrying the people's myths;
 * ``travel``/``visits`` show how many places an expedition may visit per
   turn given transport networks and technology;
 * determinism: same world + same turn + same projection ⇒ byte-identical
   JSON (§4.1), no wall-clock anywhere (§20.4).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.api import WorldServer


def populated_server() -> WorldServer:
    srv = WorldServer("w1")
    srv.handle("POST", "/api/v1/worlds/w1/civilizations",
               {"id": "c1", "name": "Приречная", "progress": 0.25})
    srv.handle("POST", "/api/v1/worlds/w1/civilizations",
               {"id": "c2", "name": "Горная", "progress": 0.3})
    srv.handle("POST", "/api/v1/worlds/w1/settlements",
               {"id": "s1", "civilization_id": "c1", "name": "Град",
                "terrain": "plain", "pop": 5000, "infrastructure": 0.4})
    srv.handle("POST", "/api/v1/worlds/w1/settlements",
               {"id": "s2", "civilization_id": "c1", "name": "Заречье",
                "terrain": "marsh"})
    srv.handle("POST", "/api/v1/worlds/w1/settlements",
               {"id": "s3", "civilization_id": "c2", "name": "Кремень",
                "terrain": "mountain"})
    return srv


def get_map(srv, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    path = "/api/v1/worlds/w1/map" + (f"?{qs}" if qs else "")
    resp = srv.handle("GET", path)
    assert resp.get("status") == 200, resp
    return resp["map"]


# ---------------------------------------------------------------------------
# route & payload shape
# ---------------------------------------------------------------------------

def test_map_route_registered():
    routes = {p.pattern for (m, p) in WorldServer._ROUTES if m == "GET"}
    assert r"^/api/v1/worlds/\{id\}/map$" in routes


def test_map_payload_shape():
    m = get_map(populated_server(), civilization_id="c1")
    for key in ("civilization_id", "projection", "tiles", "travel",
                "visits", "layers", "legend", "projections_available"):
        assert key in m, key
    assert m["civilization_id"] == "c1"
    assert m["projection"] == "equirectangular"
    assert set(m["projections_available"]) == {"equirectangular", "mercator", "conic"}
    layer_ids = [l["id"] for l in m["layers"]]
    assert layer_ids == ["relief", "settlements", "borders", "knowledge"]


def test_unknown_projection_is_400():
    resp = populated_server().handle(
        "GET", "/api/v1/worlds/w1/map?projection=azimuthal")
    assert resp["status"] == 400
    assert "projection" in resp["error"]


def test_unknown_civilization_is_400():
    resp = populated_server().handle(
        "GET", "/api/v1/worlds/w1/map?civilization_id=nope")
    assert resp["status"] == 400


def test_empty_world_returns_honest_empty_map():
    m = get_map(WorldServer("w-empty"))
    assert m["civilization_id"] is None
    assert m["tiles"] == []
    assert m["visits"]["options"] == []
    assert "карта пуста" in m["note"]


def test_default_civ_picked_without_explicit_param():
    # без civilization_id карта строится через призму первой активной цивилизации
    m = get_map(populated_server())
    assert m["civilization_id"] == "c1"


# ---------------------------------------------------------------------------
# prism of knowledge: what the player may see (§22.8)
# ---------------------------------------------------------------------------

def _settle_layer(m):
    return next(l for l in m["layers"] if l["id"] == "settlements")["items"]


def test_own_lands_described_in_detail():
    """Своя земля «известна» (не смутно) только когда её измеряет право:
    закон о переписи/налоге добавляет глубины знания (§22.8). Столица с
    дорогами и переписью — known; дальнее болотное поселение без дорог — dim."""
    srv = populated_server()
    for lid, title in (("law-census", "Перепись душ"), ("law-roads", "Устав дорог")):
        r = srv.handle("POST", "/api/v1/laws", {
            "id": lid, "civilization_id": "c1", "title": title,
            "raw_text": "Каждое поселение учитывается в переписи и на дорогах.",
            "canonical": {"kind": "tax"}})
        assert r["status"] == 200, r
    m = get_map(srv, civilization_id="c1")
    rows = {r["id"]: r for r in _settle_layer(m)}
    own = rows["s1"]
    assert own["relation"] == "own"
    assert own["knowing"] == "known"              # pop 5000 + infra 0.4 + перепись
    assert own["depth"] >= 0.62                   # порог known открыт законом
    assert own["population"] == 5000.0            # перепись своя — видна
    assert own["infrastructure"] == 0.4           # дороги свои — видны
    assert own["terrain"] == "plain"
    far = rows["s2"]                              # болото без дорог и людности
    assert far["relation"] == "own"
    assert far["knowing"] == "dim"


def test_own_land_without_law_is_dim_not_known():
    """Без закона-измерителя даже столица остаётся «смутной»: карта честна —
    знание земли даёт право, а не просто людность."""
    srv = WorldServer("w3")
    srv.handle("POST", "/api/v1/worlds/w3/civilizations",
               {"id": "c1", "name": "Приречная", "progress": 0.25})
    srv.handle("POST", "/api/v1/worlds/w3/settlements",
               {"id": "s1", "civilization_id": "c1", "name": "Град",
                "terrain": "plain", "pop": 5000, "infrastructure": 0.4})
    m = get_map(srv, civilization_id="c1")
    own = next(r for r in _settle_layer(m) if r["id"] == "s1")
    assert own["relation"] == "own"
    assert own["knowing"] == "dim"
    assert 0.28 <= own["depth"] < 0.62


def test_foreign_lands_hidden_beyond_the_word():
    m = get_map(populated_server(), civilization_id="c1")
    rows = {r["id"]: r for r in _settle_layer(m)}
    foreign = rows["s3"]
    assert foreign["relation"] == "foreign"
    # игрок не получает чужих подробностей: инфраструктура скрыта всегда
    assert foreign["infrastructure"] is None
    if foreign["knowing"] == "unknown":
        assert foreign["terrain"] == "unknown"
        assert foreign["owner"] is None
        assert foreign["population"] is None


def test_knowledge_markers_carry_myths_and_rumors():
    m = get_map(populated_server(), civilization_id="c1")
    items = next(l for l in m["layers"] if l["id"] == "knowledge")["items"]
    for it in items:
        if it["kind"] == "terra_incognita":
            assert it["text"].startswith("за пределом слова — ")
            assert len(it["text"]) > len("за пределом слова — ")
        elif it["kind"] == "rumor":
            assert len(it["text"]) > 10          # предание народа, а не заглушка


def test_tiles_have_glyphs_and_place_names():
    m = get_map(populated_server(), civilization_id="c1")
    tiles = m["tiles"]
    assert len(tiles) == 3                        # по поселению на тайл
    for t in tiles:
        assert t["knowing"] in ("known", "dim", "unknown")
        assert len(t["xy"]) == 2
        assert t["glyph"]
    own_tile = next(t for t in tiles if t["settlement_id"] == "s1")
    assert own_tile["place_name"] == "Град"
    assert own_tile["lore_name"].startswith("Град")   # эпитет по характеру места
    assert "origin" in own_tile["name_origin"] or own_tile["name_origin"]


def test_terra_incognita_cells_get_a_myth():
    from poem.mapview import MYTH_POOL
    m = get_map(populated_server(), civilization_id="c1")
    unknown = [t for t in m["tiles"] if t["knowing"] == "unknown"]
    for t in unknown:
        assert t["myth"] in MYTH_POOL


# ---------------------------------------------------------------------------
# projections: same land, different drawing — still deterministic
# ---------------------------------------------------------------------------

def test_projection_changes_tile_coordinates():
    srv = populated_server()
    eq = {t["settlement_id"]: t["xy"] for t in get_map(srv, civilization_id="c1")["tiles"]}
    mc = {t["settlement_id"]: t["xy"] for t in
          get_map(srv, civilization_id="c1", projection="mercator")["tiles"]}
    assert eq != mc                               # проекция реально влияет
    conic = get_map(srv, civilization_id="c1", projection="conic")["tiles"]
    assert len(conic) == 3


# ---------------------------------------------------------------------------
# travel budget & visits per turn (transport networks + technologies)
# ---------------------------------------------------------------------------

def test_travel_budget_open_formula():
    m = get_map(populated_server(), civilization_id="c1")
    tr = m["travel"]
    assert tr["visits_per_turn"] >= 1
    assert {"transport_tech", "roads_index", "language_unity", "explanation"} <= set(tr)


def test_roads_and_infrastructure_open_more_visits():
    """Дорога — это мили хода: выше инфраструктура/техника эпохи ⇒ больше
    посещений за ход. В эпохе Рассвета связи ещё нет, поэтому обе фикстуры
    ставим в индустриальную эпоху (железные дороги), где техника > 0."""
    from poem.chronology import Era

    def world(infra: float) -> WorldServer:
        srv = WorldServer("w2")
        srv.chronology.current_era = Era.INDUSTRIAL
        srv.handle("POST", "/api/v1/worlds/w2/civilizations",
                   {"id": "c1", "name": "Приречная", "progress": 0.6})
        srv.handle("POST", "/api/v1/worlds/w2/civilizations",
                   {"id": "c2", "name": "Горная", "progress": 0.6})
        srv.handle("POST", "/api/v1/worlds/w2/settlements",
                   {"id": "s1", "civilization_id": "c1", "name": "Град",
                    "terrain": "plain", "pop": 5000, "infrastructure": infra})
        srv.handle("POST", "/api/v1/worlds/w2/settlements",
                   {"id": "s3", "civilization_id": "c2", "name": "Кремень",
                    "terrain": "mountain"})
        return srv

    weak = get_map(world(0.0), civilization_id="c1")["travel"]
    strong = get_map(world(1.0), civilization_id="c1")["travel"]
    assert strong["roads_index"] > weak["roads_index"]
    assert strong["visits_per_turn"] >= weak["visits_per_turn"]
    # на пределе reach (1 + 3·tech + 2·roads + unity) дороги переводят 4.x → 6+ миль
    assert weak["visits_per_turn"] == int((1 + 3 * weak["transport_tech"]
                                           + 2 * weak["roads_index"]
                                           + weak["language_unity"]) // 2) or \
        weak["visits_per_turn"] == 1
    assert strong["visits_per_turn"] > weak["visits_per_turn"]


def test_visit_options_sorted_with_costs():
    m = get_map(populated_server(), civilization_id="c1")
    opts = m["visits"]["options"]
    assert [o["settlement_id"] for o in opts] == sorted(
        [o["settlement_id"] for o in opts],
        key=lambda sid: (not next(o for o in opts if o["settlement_id"] == sid)["reachable_this_turn"],
                         next(o for o in opts if o["settlement_id"] == sid)["travel_days"], sid))
    for o in opts:
        assert o["travel_days"] > 0
        if not o["reachable_this_turn"]:
            assert o["reason"]                    # честное «почему нельзя»


# ---------------------------------------------------------------------------
# determinism (§4.1) and absence of wall-clock (§20.4)
# ---------------------------------------------------------------------------

def test_map_response_is_deterministic():
    srv = populated_server()
    a = json.dumps(get_map(srv, civilization_id="c1"), sort_keys=True)
    b = json.dumps(get_map(srv, civilization_id="c1"), sort_keys=True)
    assert a == b                                 # байт-идентично в тот же ход
    c = json.dumps(get_map(srv, civilization_id="c1", projection="mercator"),
                   sort_keys=True)
    d = json.dumps(get_map(srv, civilization_id="c1", projection="mercator"),
                   sort_keys=True)
    assert c == d and a != c


def test_map_has_no_real_time_fields():
    """§20.4: карта не знает реальных часов — в payload нет ни временных
    меток, ни полей типа now/timestamp/updated_at (проверяем ключи, а не
    подстроки: кириллические слова вроде «новь» не должны давать ложных срабатываний)."""
    m = get_map(populated_server(), civilization_id="c1")

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert str(k).lower() not in {
                    "now", "timestamp", "created_at", "updated_at",
                    "real_time", "wall_clock"}, k
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(m)
    # и в JSON-сериализации нет ISO-дат реального времени (§20.4)
    import re
    payload = json.dumps(m)
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", payload)
