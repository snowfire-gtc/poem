"""Tests for the optional LLM prose layer (poem.llm, §13 step 22).

Covers:
 * configuration from environment (off by default);
 * deterministic prompt building and sha256 cache keys;
 * sanitising / safety filtering of model answers;
 * provider transports are never touched when disabled;
 * build_ruler_report integration: enabled provider rewrites sections,
   cached identical facts stay reproducible, failing provider degrades to
   the byte-identical template report.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.chronicle_report import build_ruler_report
from poem.core import CanonicalLaw, CanonicalLawChange, LawUserAction
from poem.llm import (LlmConfig, LlmTextProvider, LlmTransportError,
                      build_prompt, get_default_provider, looks_safe,
                      prompt_key, sanitize, set_default_provider)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ruler_reports import make_rich_engine

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_disabled_by_default(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("POEM_LLM"):
            monkeypatch.delenv(k, raising=False)
    cfg = LlmConfig.from_env()
    assert cfg.enabled is False
    p = LlmTextProvider(cfg)
    assert p.generate("epic", "wars", {"war_id": "w"},
                      civ_label="Rivers", fallback="шаблон") == "шаблон"
    assert p.calls == 0


def test_env_configuration(monkeypatch):
    monkeypatch.setenv("POEM_LLM_ENABLED", "1")
    monkeypatch.setenv("POEM_LLM_PROVIDER", "command")
    monkeypatch.setenv("POEM_LLM_MODEL", "llama-3")
    monkeypatch.setenv("POEM_LLM_BASE_URL", "http://localhost:11434/v1/")
    monkeypatch.setenv("POEM_LLM_TIMEOUT", "5")
    monkeypatch.setenv("POEM_LLM_STYLE", "skaldic saga")
    cfg = LlmConfig.from_env()
    assert cfg.enabled and cfg.provider == "command"
    assert cfg.model == "llama-3"
    assert cfg.base_url == "http://localhost:11434/v1"
    assert cfg.timeout == 5.0 and cfg.style == "skaldic saga"


# ---------------------------------------------------------------------------
# prompts & sanitising
# ---------------------------------------------------------------------------


def test_prompt_is_deterministic_and_fact_bearing():
    facts = {"war_id": "w1x", "years_elapsed": "8", "enemy": "Hills"}
    a = build_prompt("epic", "wars", facts, civ_label="Rivers")
    b = build_prompt("epic", "wars", dict(reversed(list(facts.items()))),
                     civ_label="Rivers")
    assert a == b                      # key order does not matter
    assert prompt_key(a) == prompt_key(b)
    assert "Hills" in a and "Rivers" in a and "эпическое" in a


def test_sanitize_strips_quotes_and_caps_lines():
    raw = '  «Первая строка\nвторая\nтретья\nчетвёртая\nпятая» \n'
    out = sanitize(raw)
    assert out.splitlines() == ["Первая строка", "вторая", "третья",
                                "четвёртая"]
    assert looks_safe(out)
    assert not looks_safe("")
    assert not looks_safe("Извините, я не могу написать это.")


# ---------------------------------------------------------------------------
# provider behaviour with an injected transport
# ---------------------------------------------------------------------------


def _provider(responder, enabled=True):
    calls = []

    def transport(cfg, prompt):
        calls.append(prompt)
        return responder(prompt)

    return LlmTextProvider(LlmConfig(enabled=enabled, model="test"),
                           transport=transport), calls


def test_enabled_provider_rewrites_and_caches():
    provider, calls = _provider(lambda p: "Свежая строка\nи вторая строка")
    facts = {"war_id": "w1x", "enemy": "Hills"}
    out = provider.generate("epic", "wars", facts, civ_label="Rivers",
                            fallback="шаблон")
    assert out == "Свежая строка\nи вторая строка"
    out2 = provider.generate("epic", "wars", facts, civ_label="Rivers",
                             fallback="шаблон")
    assert out2 == out
    assert len(calls) == 1             # second hit came from the cache
    assert provider.calls == 1


def test_transport_failure_falls_back_to_template():
    def boom(cfg, prompt):
        raise LlmTransportError("service down")
    provider, _ = _provider(boom)
    assert provider.generate("lament", "wars", {"war_id": "w"},
                             civ_label="Rivers",
                             fallback="шаблонный плач") == "шаблонный плач"


def test_unsafe_answer_falls_back():
    provider, _ = _provider(lambda p: "Извините, я не могу писать стихи.")
    assert provider.generate("lyric", "peace", {}, civ_label="Rivers",
                             fallback="мир по шаблону") == "мир по шаблону"


def test_no_network_when_disabled(monkeypatch):
    import urllib.request as ur
    monkeypatch.delenv("POEM_LLM_ENABLED", raising=False)
    called = []
    monkeypatch.setattr(ur, "urlopen",
                        lambda *a, **k: called.append(a), raising=True)
    p = get_default_provider_for_test()
    assert p.generate("epic", "wars", {"x": 1}, civ_label="c",
                      fallback="fb") == "fb"
    assert called == []


def get_default_provider_for_test():
    # fresh default provider built strictly from env (disabled here)
    set_default_provider(None)
    return get_default_provider()


# ---------------------------------------------------------------------------
# integration with build_ruler_report
# ---------------------------------------------------------------------------


def _rich_turn_inputs():
    store, chrono, engine = make_rich_engine()
    rev = store.record_revision(
        law_id="law-tax", civilization_id="a",
        action=LawUserAction.AMEND_LAW,
        raw_text="Exempt small settlements.",
        canonical_change=CanonicalLawChange(
            action=LawUserAction.AMEND_LAW,
            new_canonical=CanonicalLaw(kind="tax")),
        now=DAY)
    engine.open_turn(started_at=DAY, simulation_cutoff_at=DAY)
    report = engine.run_turn()
    return report, rev


def _sections_of(report, civ, llm=None):
    revisions_meta = []
    for snap in getattr(report, "snapshots", []):
        for r in list(snap.pending_amendments) + list(snap.pending_repeals):
            revisions_meta.append({
                "revision_id": r.id, "law_id": r.law_id,
                "civilization_id": r.civilization_id,
                "action": r.action.value})
    # the engine already composed ruler_reports with the template texts;
    # rebuild them from the same published view/report with (or without)
    # an LLM provider to compare both renderings of identical facts.
    engine_rr = next(x for x in report.ruler_reports
                     if x["civilization_id"] == civ)
    war_parties = {}
    for s in engine_rr["sections"]:
        if s["kind"] == "wars" and s["refs"].get("war_id"):
            war_parties[s["refs"]["war_id"]] = {
                "attacker_id": "a", "defender_id": "b"}
    rr = build_ruler_report(report.view, {
        "years_elapsed": report.years_elapsed,
        "era_before": report.era_before.value,
        "era_after": report.era_after.value,
        "world_year_after": report.world_year_after,
        "applied_revisions": report.applied_revisions,
        "_revisions_meta": revisions_meta,
        "_civ_names": {"a": "Rivers", "b": "Hills"},
        "_war_parties": war_parties,
    }, [{"id": "a", "name": "Rivers"}, {"id": "b", "name": "Hills"}],
        [{"folklore_id": f["folklore_id"],
          "civilization_id": f.get("civilization_id"),
          "title": f.get("title", ""),
          "cancelled": bool(f.get("cancelled"))}
         for f in report.view.get("folklore", []) if f.get("civilization_id") == civ],
        llm=llm)
    return next(x for x in rr if x["civilization_id"] == civ)


def test_llm_rewrite_appears_in_ruler_report():
    report, rev = _rich_turn_inputs()
    provider, calls = _provider(lambda p: "Новая песнь о стали и реках")
    out = _sections_of(report, "a", llm=provider)
    war_sec = next(s for s in out["sections"] if s["kind"] == "wars")
    assert war_sec["text"] == "Новая песнь о стали и реках"
    assert calls, "LLM transport was never asked"
    # every section got rewritten through the same provider
    assert all(s["text"] == "Новая песнь о стали и реках"
               for s in out["sections"])
    # the applied revision really produced a laws section on the LLM path
    assert any(s["kind"] == "laws" for s in out["sections"])


def test_llm_cache_gives_reproducible_reports():
    report, rev = _rich_turn_inputs()
    n = [0]

    def responder(prompt):
        n[0] += 1
        return f"строка {hash(prompt) % 97}"
    provider, _ = _provider(responder)
    a = json.dumps(_sections_of(report, "a", llm=provider), ensure_ascii=False)
    b = json.dumps(_sections_of(report, "a", llm=provider), ensure_ascii=False)
    assert a == b
    assert n[0] == provider.calls      # only unique prompts hit the wire
    assert provider.calls > 0


def test_failed_llm_keeps_template_report_byte_identical():
    report, rev = _rich_turn_inputs()
    baseline = json.dumps(_sections_of(report, "a"), ensure_ascii=False)
    dead, _ = _provider(lambda p: (_ for _ in ()).throw(
        LlmTransportError("down")))
    degraded = json.dumps(_sections_of(report, "a", llm=dead),
                          ensure_ascii=False)
    assert baseline == degraded
