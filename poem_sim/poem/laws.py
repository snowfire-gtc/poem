"""Server-side store for law drafts and revisions (TASK-061, §3).

This in-memory implementation mirrors the SQL schema of §14.1–14.2:

    law_revisions(id, law_id, civilization_id, action, raw_text,
                  canonical_change, sequence_number, received_at,
                  simulation_turn_id, status)
    law_drafts(id, civilization_id, target_law_id, raw_text,
               canonical_draft, status, updated_at)

Design guarantees (§20):
 * The client is NOT a storage location for legally meaningful edits —
   everything lives here (server). Drafts are reachable from any device
   by id (cross-device access = simple lookup by id in this store).
 * ``received_at`` is recorded for auditing only. It is never used as a
   gameplay modifier (TASK-060): no time-of-day cost multiplier,
   no late-edit delay chance, no soft freeze.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import replace
from datetime import datetime

from .core import (
    CanonicalLaw,
    CanonicalLawChange,
    DraftStatus,
    Law,
    LawDraft,
    LawDraftId,
    LawRevision,
    LawRevisionId,
    LawRevisionStatus,
    LawUserAction,
    TurnId,
    utcnow,
)


class LawStoreError(Exception):
    pass


class LawStore:
    """Authoritative server store for laws, drafts and revisions."""

    def __init__(self, validator=None) -> None:
        self._laws: dict[str, Law] = {}
        self._drafts: dict[LawDraftId, LawDraft] = {}
        self._revisions: dict[LawRevisionId, LawRevision] = {}
        # per-law monotonically increasing revision sequence numbers
        self._seq_counters: dict[str, itertools.count] = {}
        # history of applied versions per law (for "version history" UI, §16.2)
        self._law_history: dict[str, list[LawRevisionId]] = {}
        # TASK-066 (§3.3): the server-side neural layer that validates law
        # texts. Injectable so production can swap in a GPU-backed model;
        # defaults to the deterministic local heuristic validator.
        if validator is None:
            from .validation import DEFAULT_VALIDATOR
            validator = DEFAULT_VALIDATOR
        self.validator = validator

    # ------------------------------------------------------------------ laws

    def add_law(self, law: Law) -> Law:
        if law.id in self._laws:
            raise LawStoreError(f"law {law.id} already exists")
        self._laws[law.id] = law
        self._seq_counters[law.id] = itertools.count(1)
        self._law_history[law.id] = []
        return law

    def get_law(self, law_id: str) -> Law:
        try:
            return self._laws[law_id]
        except KeyError:
            raise LawStoreError(f"unknown law {law_id}") from None

    def laws_of(self, civilization_id: str) -> list[Law]:
        return [l for l in self._laws.values() if l.civilization_id == civilization_id]

    def replace_law(self, law: Law) -> None:
        if law.id not in self._laws:
            raise LawStoreError(f"unknown law {law.id}")
        self._laws[law.id] = law

    # ---------------------------------------------------------------- drafts

    def create_draft(
        self,
        civilization_id: str,
        raw_text: str,
        target_law_id: str | None = None,
        now: datetime | None = None,
    ) -> LawDraft:
        draft = LawDraft(
            id=str(uuid.uuid4()),
            civilization_id=civilization_id,
            target_law_id=target_law_id,
            raw_text=raw_text,
            updated_at=now or utcnow(),
        )
        self._drafts[draft.id] = draft
        return draft

    def update_draft(
        self,
        draft_id: LawDraftId,
        raw_text: str | None = None,
        canonical_draft: CanonicalLaw | None = None,
        status: DraftStatus | None = None,
        now: datetime | None = None,
    ) -> LawDraft:
        """UpdateDraft — works from any device; state lives on the server."""
        draft = self._drafts.get(draft_id)
        if draft is None:
            raise LawStoreError(f"unknown draft {draft_id}")
        if draft.status in (DraftStatus.SUBMITTED, DraftStatus.DISCARDED):
            raise LawStoreError(f"draft {draft_id} is {draft.status.value} and immutable")
        if raw_text is not None:
            draft.raw_text = raw_text
        if canonical_draft is not None:
            draft.canonical_draft = canonical_draft
            draft.status = DraftStatus.VALIDATED
        if status is not None:
            draft.status = status
        draft.updated_at = now or utcnow()
        return draft

    def validate_draft(self, draft_id: LawDraftId) -> LawDraft:
        """Run the server-side neural layer over a draft's text (§3.3).

        On success the produced canonical form is attached and the draft
        becomes ``Validated``; on failure it becomes ``Invalid`` and STAYS
        on the server so the player can keep editing it from any device
        (guide_client.md: «черновик остаётся на сервере в статусе Invalid»).
        The verdict depends only on the text — never on wall-clock time
        (§2, TASK-060): identical input yields an identical result.
        """
        draft = self.get_draft(draft_id)
        if draft.status in (DraftStatus.SUBMITTED, DraftStatus.DISCARDED):
            raise LawStoreError(f"draft {draft_id} is {draft.status.value} and immutable")
        result = self.validator.validate(draft.raw_text, draft.target_law_id)
        if result.ok:
            draft.canonical_draft = result.canonical
            draft.status = DraftStatus.VALIDATED
        else:
            draft.status = DraftStatus.INVALID
        return draft

    def get_draft(self, draft_id: LawDraftId) -> LawDraft:
        try:
            return self._drafts[draft_id]
        except KeyError:
            raise LawStoreError(f"unknown draft {draft_id}") from None

    def list_drafts(self, civilization_id: str) -> list[LawDraft]:
        return [
            d for d in self._drafts.values()
            if d.civilization_id == civilization_id
            and d.status != DraftStatus.DISCARDED
        ]

    def discard_draft(self, draft_id: LawDraftId) -> None:
        draft = self.get_draft(draft_id)
        if draft.status == DraftStatus.SUBMITTED:
            raise LawStoreError("submitted drafts cannot be discarded")
        draft.status = DraftStatus.DISCARDED

    # -------------------------------------------------------------- revisions

    def submit_draft(self, draft_id: LawDraftId, now: datetime | None = None) -> LawRevision:
        """SubmitDraft: convert a validated draft into an official revision."""
        draft = self.get_draft(draft_id)
        if draft.status not in (DraftStatus.VALIDATED, DraftStatus.READY_FOR_PREVIEW):
            raise LawStoreError(
                f"draft {draft_id} has status {draft.status.value}; "
                "it must be Validated before submission"
            )
        if draft.target_law_id is None:
            raise LawStoreError("draft has no target law")
        change = CanonicalLawChange(
            action=LawUserAction.AMEND_LAW,
            new_canonical=draft.canonical_draft,
        )
        revision = self.record_revision(
            law_id=draft.target_law_id,
            civilization_id=draft.civilization_id,
            action=LawUserAction.SUBMIT_DRAFT,
            raw_text=draft.raw_text,
            canonical_change=change,
            now=now,
        )
        draft.status = DraftStatus.SUBMITTED
        return revision

    def record_revision(
        self,
        law_id: str,
        civilization_id: str,
        action: LawUserAction,
        raw_text: str,
        canonical_change: CanonicalLawChange,
        now: datetime | None = None,
    ) -> LawRevision:
        """AmendLaw / RepealLaw / SuspendLaw / ReinstateLaw / ClarifyLaw entry point.

        The revision is stored immediately (Pending). Its acceptance into a
        turn is decided solely by the logical cutoff comparison in
        ``TurnEngine`` — never by wall-clock time-of-day (§2, TASK-060).
        """
        law = self.get_law(law_id)
        if law.civilization_id != civilization_id:
            raise LawStoreError("civilization mismatch for revision")
        seq = next(self._seq_counters[law_id])
        revision = LawRevision(
            id=str(uuid.uuid4()),
            law_id=law_id,
            civilization_id=civilization_id,
            action=action,
            raw_text=raw_text,
            canonical_change=canonical_change,
            sequence_number=seq,
            received_at=now or utcnow(),   # audit only, never a gameplay modifier
            status=LawRevisionStatus.PENDING,
        )
        self._revisions[revision.id] = revision
        law.last_revision_seq = seq
        return revision

    def cancel_pending_change(self, revision_id: LawRevisionId) -> LawRevision:
        """CancelPendingChange (§3.2)."""
        rev = self.get_revision(revision_id)
        if rev.status != LawRevisionStatus.PENDING:
            raise LawStoreError(f"revision {revision_id} is {rev.status.value}, not pending")
        rev.status = LawRevisionStatus.SUPERSEDED
        rev.canonical_change = replace(
            rev.canonical_change, note="cancelled by user via CancelPendingChange"
        )
        return rev

    def get_revision(self, revision_id: LawRevisionId) -> LawRevision:
        try:
            return self._revisions[revision_id]
        except KeyError:
            raise LawStoreError(f"unknown revision {revision_id}") from None

    def revisions_for_law(self, law_id: str) -> list[LawRevision]:
        return sorted(
            (r for r in self._revisions.values() if r.law_id == law_id),
            key=lambda r: r.sequence_number,
        )

    def pending_revisions(
        self,
        civilization_id: str | None = None,
        law_id: str | None = None,
    ) -> list[LawRevision]:
        out = [
            r for r in self._revisions.values()
            if r.status == LawRevisionStatus.PENDING
        ]
        if civilization_id is not None:
            out = [r for r in out if r.civilization_id == civilization_id]
        if law_id is not None:
            out = [r for r in out if r.law_id == law_id]
        return sorted(out, key=lambda r: (r.law_id, r.sequence_number))

    def mark_included(self, revision_id: LawRevisionId, turn_id: TurnId) -> None:
        rev = self.get_revision(revision_id)
        rev.simulation_turn_id = turn_id
        rev.status = LawRevisionStatus.INCLUDED_IN_SIMULATION

    def mark_superseded(self, revision_id: LawRevisionId) -> None:
        self.get_revision(revision_id).status = LawRevisionStatus.SUPERSEDED

    def mark_applied(self, revision_id: LawRevisionId) -> None:
        rev = self.get_revision(revision_id)
        rev.status = LawRevisionStatus.APPLIED
        self._law_history.setdefault(rev.law_id, []).append(revision_id)

    def mark_rejected(self, revision_id: LawRevisionId, reason: str = "") -> None:
        rev = self.get_revision(revision_id)
        rev.status = LawRevisionStatus.REJECTED
        if reason:
            rev.canonical_change = replace(rev.canonical_change, note=reason)

    def mark_failed(self, revision_id: LawRevisionId, reason: str = "") -> None:
        """§3.4 ``Failed`` — the revision joined a simulation slice but its
        application raised; it stays in history for audit and is never
        retried silently (the player sees the verdict via §15.3/§16.2)."""
        rev = self.get_revision(revision_id)
        rev.status = LawRevisionStatus.FAILED
        if reason:
            rev.canonical_change = replace(rev.canonical_change, note=reason)

    def version_history(self, law_id: str) -> list[LawRevision]:
        """Full version history for UI (§16.2): includes superseded versions."""
        return self.revisions_for_law(law_id)
