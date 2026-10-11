"""Tests for TASK-067 — visual identity token memory in server RAM.

Covers:
 * the registry itself (idempotent registration by (kind, name), field-wise
   refinement, deterministic search, related-token consistency context);
 * prompt conditioning built from tokens (faces of historical persons and
   era style folded into chronicle illustration prompts, §13 step 20);
 * determinism: same inputs -> byte-identical prompts regardless of
   wall-clock time (§20.4);
 * the REST routes /worlds/{id}/tokens, /tokens/{id}, /tokens/{id}/condition
   and /images/{id}/condition;
 * turn integration: engine.tokens feeds queue_image_tasks so recurring
   subjects keep the same look across illustrations.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.api import WorldServer
from poem.chronology import Civilization, WorldChronology
from poem.core import CanonicalLaw, Law, LawUserAction
from poem.laws import LawStore
from poem.tokens import TOKEN_KINDS, TokenRegistry, VisualToken
from poem.turn import TurnEngine
from poem.views import enrich_prompt, queue_image_tasks, select_tokens_for_task


# ---------------------------------------------------------------------------
# Registry unit behaviour
# ---------------------------------------------------------------------------

def test_unknown_kind_rejected():
    reg = TokenRegistry("w1")
    try:
        VisualToken(id="t1", kind="spaceship", name="X")
        raise AssertionError("should have raised")
    except ValueError as exc:
        assert "spaceship" in str(exc)
    assert set(TOKEN_KINDS) >= {"person", "invention", "painting"}


def test_registration_is_idempotent_by_kind_and_name():
    reg = TokenRegistry("w1")
    a = reg.register({"kind": "person", "name": "Юлий Цезарь",
                      "descriptor": "орлиный взгляд, лавровый венок"})
    b = reg.register({"kind": "person", "name": "юлий цезарь",   # case/ё-insensitive
                      "descriptor": "резкие черты, короткая борода"})
    assert a.id == b.id                     # refined, not duplicated
    assert len(reg) == 1
    assert reg.get(a.id).descriptor == "резкие черты, короткая борода"
    assert reg.get(a.id).world_id == "w1"


def test_register_update_merges_fields_absent_keys_kept():
    reg = TokenRegistry()
    t = reg.register({"kind": "painting", "name": "Мона Лиза",
                      "descriptor": "улыбка, сфумато", "tags": ["портрет"],
                      "asset_ref": "assets/ref/mona.jpg"})
    upd = reg.register({"id": t.id, "kind": "painting", "name": "Мона Лиза",
                        "refs": {"folklore_id": "f1"}})
    assert upd.descriptor == "улыбка, сфумато"      # untouched
    assert upd.asset_ref == "assets/ref/mona.jpg"   # untouched
    assert upd.refs == {"folklore_id": "f1"}
    # changing kind of an existing id is refused (one identity, one kind)
    try:
        reg.register({"id": t.id, "kind": "creature", "name": "Мона Лиза"})
        raise AssertionError("should have raised")
    except ValueError as exc:
        assert "cannot change kind" in str(exc)


def test_search_by_query_kind_tag_is_deterministic():
    reg = TokenRegistry()
    reg.register({"kind": "person", "name": "Клеопатра",
                  "descriptor": "тёмные глаза, золотая диадема",
                  "tags": ["египет", "царствующий дом"]})
    reg.register({"kind": "place", "name": "Александрия",
                  "descriptor": "маяк Фарос, белые кварталы",
                  "tags": ["египет"]})
    reg.register({"kind": "invention", "name": "Анемометр",
                  "descriptor": "латунные чашечки на спицах"})
    assert [t.name for t in reg.search("египет")] == ["Клеопатра", "Александрия"]
    assert [t.name for t in reg.search(kind="invention")] == ["Анемометр"]
    assert [t.name for t in reg.search(tag="Египет")] == ["Клеопатра", "Александрия"]
    assert reg.search("несущественное имя") == []


def test_related_to_shared_tags_and_refs():
    reg = TokenRegistry()
    cleo = reg.register({"kind": "person", "name": "Клеопатра", "tags": ["египет"]})
    crown = reg.register({"kind": "clothing", "name": "Псешет",
                          "descriptor": "двойная корона", "tags": ["египет"]})
    mask = reg.register({"kind": "invention", "name": "Погребальная маска",
                         "refs": {"law_id": "law-burial"}})
    law_tok = reg.register({"kind": "event", "name": "Закон о погребении",
                            "refs": {"law_id": "law-burial"}})
    rel = reg.related_to(cleo)
    assert crown in rel          # shared tag «египет»
    assert mask not in rel       # unrelated
    assert reg.related_to(mask) == [law_tok]  # linked via law_id ref


def test_condition_for_aggregates_prompt_refs_negative():
    reg = TokenRegistry()
    p = reg.register({"kind": "person", "name": "Архимед",
                      "descriptor": "седая борода, задумчивый взгляд",
                      "asset_ref": "assets/ref/archimedes.png",
                      "metadata": {"negative": "modern watch, photoreal skin"}})
    cond = reg.condition_for([p.id, "missing-id"])
    assert cond["prompt_fragments"] == ["person: Архимед — седая борода, задумчивый взгляд"]
    assert cond["references"] == ["assets/ref/archimedes.png"]
    assert cond["negative"] == ["modern watch, photoreal skin"]
    assert cond["tokens"][0]["id"] == p.id     # unknown ids silently skipped


# ---------------------------------------------------------------------------
# Prompt enrichment (views layer)
# ---------------------------------------------------------------------------

VIEW = {
    "dashboard": {"era_value": "classical", "current_year": 25.0, "turn": 1},
    "events": [{"title": "Реформа налогов", "detail": "Юлий Цезарь ввёл поземельный налог."}],
    "laws": [{"id": "law-tax", "civilization_id": "c1", "title": "Налог"}],
    "folklore": [],
    "applied_revisions": ["law-tax"],
}


def _registry_with_faces():
    reg = TokenRegistry("w1")
    reg.register({"kind": "person", "name": "Юлий Цезарь",
                  "descriptor": "лысеющий лоб, орлиный нос"})
    reg.register({"kind": "style", "name": "Классическая школа",
                  "descriptor": "идеализированные профили",
                  "refs": {"era": "classical"}})
    return reg


def test_enrich_prompt_adds_era_style_and_face():
    reg = _registry_with_faces()
    prompt, ids = enrich_prompt("base prompt", {"kind": "hero"}, VIEW, reg)
    assert "art style: balanced marble colonnades" in prompt
    assert "Юлий Цезарь — лысеющий лоб, орлиный нос" in prompt
    assert len(ids) == 2


def test_select_tokens_picks_person_mentioned_in_chronicle():
    reg = _registry_with_faces()
    picked = select_tokens_for_task(reg, {"kind": "hero"}, VIEW)
    names = [t.name for t in picked]
    assert "Юлий Цезарь" in names and "Классическая школа" in names
    # no registry -> no tokens, prompt unchanged apart from style
    none_ids = select_tokens_for_task(None, {}, VIEW)
    assert none_ids == []


def test_token_conditioning_is_time_independent():
    """§20.4: prompts derived from tokens never mention wall-clock data."""
    reg = _registry_with_faces()
    p1, ids1 = enrich_prompt("x", {}, VIEW, reg)
    p2, ids2 = enrich_prompt("x", {}, VIEW, reg)
    assert p1 == p2 and ids1 == ids2


# ---------------------------------------------------------------------------
# Turn integration: engine-owned registry conditions queued tasks
# ---------------------------------------------------------------------------

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


def make_server(with_token=True):
    srv = WorldServer("w1")
    srv.add_civilization(Civilization(id="c1", name="Rivers", progress=0.25,
                                      annual_production_rate=100.0))
    srv.store.add_law(Law(
        id="law-tax", civilization_id="c1", title="Head tax",
        raw_text="Every settlement pays 1 unit per hearth.",
        canonical=CanonicalLaw(kind="tax"), complexity=1.5, tradition=0.2))
    if with_token:
        # Cross-linked to the world's tax law: when its revision is applied,
        # the hero chronicle scene (and the law vignette) get this face.
        srv.engine.tokens.register({
            "kind": "person", "name": "Цезарь",
            "descriptor": "лавровый венок, проницательный взгляд",
            "refs": {"law_id": "law-tax"}})
    return srv


def run_turn(srv):
    srv.engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY.replace(hour=23))
    return srv.engine.run_turn()


def test_engine_queues_tasks_with_token_ids():
    srv = make_server()
    run_turn(srv)
    hero = next(t for t in srv.engine.image_queue if t.payload.get("kind") == "hero")
    tok = srv.engine.tokens.find_by_name("Цезарь", kind="person")
    assert tok.id in hero.payload["token_ids"]
    assert "Цезарь — лавровый венок" in hero.prompt


def test_queue_without_registry_still_works():
    srv = make_server(with_token=False)
    run_turn(srv)
    hero = next(t for t in srv.engine.image_queue if t.payload.get("kind") == "hero")
    assert "token_ids" not in hero.payload
    assert "art style:" in hero.prompt      # era style comes from view alone


# ---------------------------------------------------------------------------
# REST API surface
# ---------------------------------------------------------------------------

def test_api_register_list_get_update_delete_token():
    srv = make_server(with_token=False)
    r = srv.handle("POST", "/api/v1/worlds/w1/tokens",
                   {"kind": "invention", "name": "Водяной винт",
                    "descriptor": "бревенчатый винт в кирпичной башне",
                    "tags": ["ирригация"], "civilization_id": "c1"})
    assert r["status"] == 200
    tid = r["token"]["id"]

    r = srv.handle("GET", "/api/v1/worlds/w1/tokens", {"q": "винт"})
    assert r["count"] == 1 and r["tokens"][0]["id"] == tid

    r = srv.handle("GET", f"/api/v1/tokens/{tid}", {})
    assert r["token"]["descriptor"].startswith("бревенчатый")

    r = srv.handle("PUT", f"/api/v1/tokens/{tid}", {"descriptor": "бронзовый винт Архимеда"})
    assert r["token"]["id"] == tid
    assert r["token"]["descriptor"] == "бронзовый винт Архимеда"
    assert r["token"]["tags"] == ["ирригация"]   # merged, not replaced

    r = srv.handle("GET", f"/api/v1/tokens/{tid}/condition", {})
    cond = r["condition"]
    assert cond["token_id"] == tid
    assert any("бронзовый винт" in f for f in cond["prompt_fragments"])

    assert srv.handle("DELETE", f"/api/v1/tokens/{tid}", {})["deleted"] == tid
    bad = srv.handle("GET", f"/api/v1/tokens/{tid}", {})
    assert bad["status"] == 400


def test_api_rejects_bad_payloads():
    srv = make_server(with_token=False)
    r = srv.handle("POST", "/api/v1/worlds/w1/tokens", {"kind": "person", "name": ""})
    assert r["status"] == 400 and "name" in r["error"]
    r = srv.handle("POST", "/api/v1/worlds/w1/tokens", {"kind": "ufo", "name": "Блюдце"})
    assert r["status"] == 400 and "ufo" in r["error"]


def test_api_task_condition_returns_renderer_bundle():
    srv = make_server()
    run_turn(srv)   # run_turn already extends engine.image_queue (§13 st.20)
    hero = next(t for t in srv.engine.image_queue if t.payload.get("kind") == "hero")
    r = srv.handle("GET", f"/api/v1/images/{hero.id}/condition", {})
    assert r["status"] == 200
    cond = r["condition"]
    assert cond["task_id"] == hero.id
    assert cond["prompt"] == hero.prompt
    assert cond["tokens"][0]["name"] == "Цезарь"


def test_law_card_exposes_bound_visual_tokens():
    srv = make_server(with_token=False)
    srv.handle("POST", "/api/v1/worlds/w1/tokens",
               {"kind": "painting", "name": "Триумф налогов",
                "refs": {"law_id": "law-tax"}})
    detail = srv.handle("GET", "/api/v1/laws/law-tax", {})
    names = [t["name"] for t in detail["visual_tokens"]]
    assert "Триумф налогов" in names
