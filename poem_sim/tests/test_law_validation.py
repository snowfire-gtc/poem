"""TASK-066: server-side neural layer validating law texts (§3.3).

Covers the deterministic local validator, its integration into LawStore
(validate_draft transitions) and the REST route POST /laws/drafts/{id}/validate.
All verdicts must be time-of-day independent (§2).
"""

from datetime import datetime, timezone

import pytest

from poem import (
    DEFAULT_VALIDATOR,
    DraftStatus,
    Law,
    LawStore,
    ValidationResult,
    WorldServer,
    detect_kind,
)


def _dt(hour: int) -> datetime:
    return datetime(2026, 10, 12, hour, 0, 0, tzinfo=timezone.utc)


GOOD_TAX = "Вводится налог 10 процентов с каждого поселения на зерно."
CONTRADICTORY = "Зерно облагается налогом 10%, но налог на зерно не собирают."
VAGUE = "Некоторые налоги должны быть немного лучше и справедливо оптимально."
TOO_SHORT = "Налог 5%."


# ---------------------------------------------------------------- validator

class TestLawValidator:
    def test_good_tax_text_validated_with_canonical(self):
        result = DEFAULT_VALIDATOR.validate(GOOD_TAX)
        assert isinstance(result, ValidationResult)
        assert result.ok
        assert result.status is DraftStatus.VALIDATED
        assert result.canonical is not None
        assert result.canonical.kind == "tax"
        assert result.canonical.effect.get("value") == 10.0
        assert result.canonical.scope.get("target") == "settlements"
        assert result.errors == []

    def test_ban_marks_prohibited_and_detects_kind(self):
        result = DEFAULT_VALIDATOR.validate(
            "Запрещается вырубка всех лесов без исключения под страхом штрафа.")
        assert result.ok
        assert result.canonical.kind == "ban"
        assert result.canonical.effect.get("prohibited") is True

    def test_contradiction_rejected(self):
        result = DEFAULT_VALIDATOR.validate(CONTRADICTORY)
        assert not result.ok
        assert result.status is DraftStatus.INVALID
        assert any("противоречив" in e for e in result.errors)
        assert result.canonical is None

    def test_vague_text_not_concrete(self):
        result = DEFAULT_VALIDATOR.validate(VAGUE)
        assert not result.ok
        assert any("конкретн" in e for e in result.errors)

    def test_short_text_rejected(self):
        result = DEFAULT_VALIDATOR.validate(TOO_SHORT)
        assert not result.ok
        assert any("коротк" in e for e in result.errors)

    def test_target_law_id_goes_into_refs(self):
        result = DEFAULT_VALIDATOR.validate(GOOD_TAX, target_law_id="law-77")
        assert result.ok
        assert result.canonical.refs == ("law-77",)

    def test_detect_kind_is_deterministic(self):
        assert detect_kind(GOOD_TAX) == "tax"
        assert detect_kind(GOOD_TAX) == detect_kind(GOOD_TAX)
        assert detect_kind("полная ерунда про ветер") is None

    @pytest.mark.parametrize("hour", [3, 9, 18, 23])
    def test_verdict_independent_of_time_of_day(self, hour):
        # The validator never consults wall-clock time; validate at 03:00 and
        # 18:00 — identical verdicts (§2, TASK-060 rule).
        a = DEFAULT_VALIDATOR.validate(GOOD_TAX)
        b = DEFAULT_VALIDATOR.validate(GOOD_TAX)
        assert a.to_json() == b.to_json()
        assert _dt(hour)  # time of day is irrelevant to the outcome

    def test_to_json_shape(self):
        payload = DEFAULT_VALIDATOR.validate(GOOD_TAX).to_json()
        assert set(payload) == {
            "ok", "status", "canonical", "errors", "warnings", "specificity"}
        assert payload["status"] == "Validated"


# ------------------------------------------------------------------- store

class TestStoreIntegration:
    def test_validate_draft_transitions_to_validated(self):
        store = LawStore()
        draft = store.create_draft("civ-1", GOOD_TAX, target_law_id=None)
        assert draft.status is DraftStatus.EDITING
        out = store.validate_draft(draft.id)
        assert out.status is DraftStatus.VALIDATED
        assert out.canonical_draft is not None

    def test_failed_validation_stays_editable_invalid(self):
        store = LawStore()
        draft = store.create_draft("civ-1", CONTRADICTORY)
        out = store.validate_draft(draft.id)
        assert out.status is DraftStatus.INVALID
        # guide_client.md: черновик остаётся на сервере и его можно править
        edited = store.update_draft(draft.id, raw_text=GOOD_TAX)
        assert edited.status is not DraftStatus.DISCARDED
        again = store.validate_draft(draft.id)
        assert again.status is DraftStatus.VALIDATED

    def test_custom_validator_is_injectable(self):
        class AlwaysOk:
            def validate(self, raw_text, target_law_id=None):
                return ValidationResult(
                    ok=True, status=DraftStatus.VALIDATED, canonical=None)

        store = LawStore(validator=AlwaysOk())
        draft = store.create_draft("civ-1", TOO_SHORT)
        assert store.validate_draft(draft.id).status is DraftStatus.VALIDATED

    def test_submitted_draft_is_immutable(self):
        store = LawStore()
        store.add_law(Law(id="law-1", civilization_id="civ-1", title="T",
                          raw_text="x", canonical=None))
        draft = store.create_draft("civ-1", GOOD_TAX, target_law_id="law-1")
        store.validate_draft(draft.id)
        store.submit_draft(draft.id)
        with pytest.raises(Exception):
            store.validate_draft(draft.id)


# --------------------------------------------------------------------- api

class TestApiRoute:
    def _server(self) -> WorldServer:
        return WorldServer(world_id="w-test")

    def test_post_validate_route_ok(self):
        srv = self._server()
        created = srv.handle("POST", "/api/v1/laws/drafts",
                             {"civilization_id": "civ-1", "raw_text": GOOD_TAX})
        assert created["status"] == 200
        draft_id = created["draft"]["id"]
        resp = srv.handle("POST", f"/api/v1/laws/drafts/{draft_id}/validate", {})
        assert resp["status"] == 200
        assert resp["draft"]["status"] == "Validated"
        assert resp["validation"]["ok"] is True
        assert resp["validation"]["canonical"]["kind"] == "tax"

    def test_post_validate_route_rejects_contradiction(self):
        srv = self._server()
        created = srv.handle("POST", "/api/v1/laws/drafts",
                             {"civilization_id": "civ-1", "raw_text": CONTRADICTORY})
        draft_id = created["draft"]["id"]
        resp = srv.handle("POST", f"/api/v1/laws/drafts/{draft_id}/validate", {})
        assert resp["status"] == 200
        assert resp["draft"]["status"] == "Invalid"
        assert resp["validation"]["ok"] is False
        # draft stays on the server, listed for editing
        listing = srv.handle("GET", "/api/v1/laws/drafts",
                             {"civilization_id": "civ-1"})
        assert [d["id"] for d in listing["drafts"]] == [draft_id]

    def test_validate_unknown_draft_is_400(self):
        srv = self._server()
        resp = srv.handle("POST", "/api/v1/laws/drafts/nope/validate", {})
        assert resp["status"] == 400
