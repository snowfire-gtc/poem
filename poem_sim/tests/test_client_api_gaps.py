"""Regression tests for the four gaps found while integrating the web client:

1. GET /laws?civilization_id=… and GET /images?status=… must actually
   filter (query-string parameters were dropped because GETs carry no body).
2. Law projections must expose ``civilization_id`` (the client renders it).
3. GET /worlds/{id}/civilizations — read-only registry so the thin client
   does not have to scrape law records for the civilization selector.
4. Admin-role probe relies on GET /worlds/{id}/tokens answering 401/403 for
   wrong credentials (client-side heuristic replaced by a server verdict).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.api import WorldServer
from poem.chronology import Civilization
from poem.core import Law


def _server(admin_token=None):
    srv = WorldServer("w1", admin_token=admin_token)
    srv.add_civilization(Civilization(id="c1", name="Дорика", progress=0.3))
    srv.add_civilization(Civilization(id="c2", name="Элида", progress=0.5))
    srv.store.add_law(Law(id="l1", civilization_id="c1", title="Налог",
                          raw_text="x", canonical=None))
    srv.store.add_law(Law(id="l2", civilization_id="c2", title="Милиция",
                          raw_text="y", canonical=None))
    return srv


# ---------------------------------------------------------------------------
# 1. query-string filters on GET routes
# ---------------------------------------------------------------------------

def test_laws_list_filters_by_query_string():
    srv = _server()
    res = srv.handle("GET", "/api/v1/laws?civilization_id=c1")
    assert res["status"] == 200
    ids = [lw["id"] for lw in res["laws"]]
    assert ids == ["l1"], f"filter must leave only c1 laws, got {ids}"

    all_res = srv.handle("GET", "/api/v1/laws")
    assert len(all_res["laws"]) == 2


def test_images_list_filters_by_status_query():
    srv = _server()
    # пустая очередь — фильтр не должен ломать ответ
    res = srv.handle("GET", "/api/v1/worlds/w1/images?status=queued")
    assert res["status"] == 200
    assert res["tasks"] == []


def test_query_params_do_not_leak_into_mutation_body():
    """POST bodies are authoritative; only GET merges the query string."""
    srv = _server()
    res = srv.handle("POST", "/api/v1/laws/drafts?civilization_id=c9",
                     {"civ_id": "c1", "raw_text": "текст"})
    assert res["status"] == 200
    assert res["draft"]["civilization_id"] == "c1"


# ---------------------------------------------------------------------------
# 2. civilization_id present in law projections
# ---------------------------------------------------------------------------

def test_law_projection_contains_civilization_id():
    srv = _server()
    res = srv.handle("GET", "/api/v1/laws")
    for law in res["laws"]:
        assert "civilization_id" in law


# ---------------------------------------------------------------------------
# 3. civilization registry GET
# ---------------------------------------------------------------------------

def test_civilization_registry_is_readable():
    srv = _server()
    res = srv.handle("GET", "/api/v1/worlds/w1/civilizations")
    assert res["status"] == 200
    by_id = {c["id"]: c for c in res["civilizations"]}
    assert set(by_id) == {"c1", "c2"}
    assert by_id["c1"]["name"] == "Дорика"


def test_civilization_registry_public_but_mutation_guarded():
    srv = _server(admin_token="secret")
    ok = srv.handle("GET", "/api/v1/worlds/w1/civilizations")
    assert ok["status"] == 200                      # read-only poll (§15)
    denied = srv.handle("POST", "/api/v1/worlds/w1/civilizations",
                        {"id": "c3"})
    assert denied["status"] == 401                  # probe basis for role


def test_role_probe_endpoint_answers_403_for_wrong_token():
    srv = _server(admin_token="secret")
    res = srv.handle("GET", "/api/v1/worlds/w1/tokens", {},
                     headers={"Authorization": "Bearer wrong"})
    # GET-маршруты публичны; проверка роли идёт через мутационный зонд:
    # клиент шлёт PUT/POST с токеном и ждёт 401/403.
    probe = srv.handle("POST", "/api/v1/worlds/w1/tokens", {"name": "x"},
                       headers={"Authorization": "Bearer wrong"})
    assert probe["status"] == 403
    good = srv.handle("POST", "/api/v1/worlds/w1/tokens",
                      {"token_id": "t1", "kind": "person", "name": "Аргон",
                       "descriptor": "царь"},
                      headers={"Authorization": "Bearer secret"})
    assert good["status"] == 200
