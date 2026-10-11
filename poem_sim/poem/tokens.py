"""Visual identity tokens of the world — server RAM registry (TASK-067).

Chronicle illustrations (§13 step 20) look plausible only when recurring
subjects keep the same face: the historical person in every portrait is the
*same* person, the invented device has one consistent look across scenes, a
painting referenced by folklore appears as the painting it is. Diffusion
renderers on the server's own GPUs consume these entries as conditioning
tokens — LoRA/Textual-Inversion identifiers, reference-image paths, IP-Adapter
embeddings or negative prompts — hence the name.

Rules honoured here:
 * State lives on the server (rules.md §20 rule 1): the registry is plain
   in-memory state owned by :class:`poem.turn.TurnEngine`, exactly like the
   image task queue; clients read it through the API and never keep their
   own copy.
 * Everything stored is derived from GAME time / deterministic data (§20.4):
   ``born_year``/``died_year`` are world years, prompt fragments contain no
   wall-clock information, so re-running a turn with the same inputs yields
   byte-identical prompts (determinism, §4.1).
 * No external services (§20.7): reference images live on the server's own
   disk (``asset_ref``, same sandboxed-path rules as rendered assets);
   embeddings are base64 blobs produced by the server's own neural layers.

Token kinds (``kind``):
 * ``person``       — historical/famous figure (face descriptor + refs)
 * ``invention``    — appearance of a device/technology
 * ``painting``     — artwork existing in the world
 * ``event``        — scene composition for a chronicle event
 * ``place``        — settlement/landmark look
 * ``creature``     — mythical/fauna creature
 * ``style``        — artistic style token (era aesthetics, school)
 * ``clothing``     — costume/attire of an era or civ
"""

from __future__ import annotations

import uuid

#: Kinds accepted by the registry; kept lowercase for stable comparisons.
TOKEN_KINDS = ("person", "invention", "painting", "event", "place",
               "creature", "style", "clothing")


class VisualToken:
    """One visual-identity entry of the world's image-conditioning memory.

    Deliberately a plain dict-backed object (like :class:`poem.views.ImageTask`'s
    payload): renderers serialize it into their local pipeline without the
    core importing any inference stack.
    """

    __slots__ = ("id", "kind", "name", "world_id", "civilization_id",
                 "descriptor", "tags", "refs", "embedding", "asset_ref",
                 "metadata", "created_at", "updated_at")

    def __init__(
        self,
        id: str,
        kind: str,
        name: str,
        world_id: str | None = None,
        civilization_id: str | None = None,
        descriptor: str = "",
        tags: list[str] | None = None,
        refs: dict | None = None,
        embedding: str | None = None,   # base64 float vector (local model output)
        asset_ref: str | None = None,   # server-local reference image path
        metadata: dict | None = None,
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> None:
        if kind not in TOKEN_KINDS:
            raise ValueError(
                f"unknown visual token kind {kind!r}; expected one of {TOKEN_KINDS}")
        self.id = str(id)
        self.kind = kind
        self.name = str(name)
        self.world_id = world_id
        self.civilization_id = civilization_id
        # Human-readable appearance fragment folded into generation prompts
        # (e.g. "sharp aquiline nose, deep-set grey eyes, cropped grey beard").
        self.descriptor = str(descriptor)
        self.tags = [str(t) for t in (tags or [])]
        # Cross-links to simulation entities: {"law_id": ...} / {"folklore_id": ...}
        # / {"event_kind": ...} / {"era": ...} — used to auto-select tokens per task.
        self.refs = dict(refs or {})
        self.embedding = embedding
        self.asset_ref = asset_ref
        self.metadata = dict(metadata or {})
        self.created_at = created_at
        self.updated_at = updated_at

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "world_id": self.world_id,
            "civilization_id": self.civilization_id,
            "descriptor": self.descriptor,
            "tags": list(self.tags),
            "refs": dict(self.refs),
            "embedding": self.embedding,
            "asset_ref": self.asset_ref,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> "VisualToken":
        return cls(
            id=data["id"], kind=data["kind"], name=data.get("name", ""),
            world_id=data.get("world_id"),
            civilization_id=data.get("civilization_id"),
            descriptor=data.get("descriptor", ""),
            tags=data.get("tags"), refs=data.get("refs"),
            embedding=data.get("embedding"), asset_ref=data.get("asset_ref"),
            metadata=data.get("metadata"),
            created_at=data.get("created_at"), updated_at=data.get("updated_at"),
        )


def _norm(text: str) -> str:
    return " ".join(str(text).lower().replace("\u0451", "е").split())


class TokenRegistry:
    """In-memory store of visual identity tokens for one world.

    O(1) lookup by id, plus deterministic search by name/kind/tag so both
    the prompt builder and the HTTP API resolve the same entries. Registration
    by name is idempotent (re-registering an existing name updates it), which
    keeps lore stable across reconnects and device switches.
    """

    def __init__(self, world_id: str | None = None) -> None:
        self.world_id = world_id
        self._by_id: dict[str, VisualToken] = {}
        self._order: list[str] = []          # insertion order → deterministic listing

    # ------------------------------------------------------------- basics

    def __len__(self) -> int:
        return len(self._by_id)

    def all(self) -> list[VisualToken]:
        return [self._by_id[tid] for tid in self._order]

    def get(self, token_id: str) -> VisualToken | None:
        return self._by_id.get(token_id)

    def must_get(self, token_id: str) -> VisualToken:
        tok = self._by_id.get(token_id)
        if tok is None:
            raise ValueError(f"unknown visual token: {token_id}")
        return tok

    # ---------------------------------------------------------- registration

    def register(self, data: dict, *, now: float | None = None) -> VisualToken:
        """Create or update a token from an API-style dict.

        ``id`` is optional: without it the token is looked up by
        ``(kind, name)`` — registering «Юлий Цезарь» twice refines the same
        entry instead of duplicating the face. ``now`` is a wall-clock stamp
        for bookkeeping only; it never feeds prompts or verdicts (§20.4).
        """
        kind = str(data.get("kind", "")).strip().lower()
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("visual token requires a name")
        token_id = str(data["id"]) if data.get("id") else None
        token = None
        if token_id and token_id in self._by_id:
            token = self._by_id[token_id]
            if "kind" in data and token.kind != kind:
                raise ValueError(
                    f"token {token_id} is a {token.kind!r}, cannot change kind to {kind!r}")
        else:
            token = self.find_by_name(name, kind=kind or None)
        if token is None:
            token = VisualToken(
                id=token_id or str(uuid.uuid4()), kind=kind, name=name,
                world_id=self.world_id or data.get("world_id"),
                created_at=now, updated_at=now)
            self._by_id[token.id] = token
            self._order.append(token.id)
        else:
            if token_id and token.id != token_id:
                raise ValueError(
                    f"name {name!r} already registered as token {token.id}")
            token.updated_at = now
        # Field-wise merge: absent keys leave prior values untouched.
        for attr in ("descriptor", "embedding", "asset_ref"):
            if data.get(attr) is not None:
                setattr(token, attr, str(data[attr]))
        if data.get("tags") is not None:
            token.tags = [str(t) for t in data["tags"]]
        if data.get("refs") is not None:
            token.refs.update(data["refs"])
        if data.get("metadata") is not None:
            token.metadata.update(data["metadata"])
        if data.get("civilization_id") is not None:
            token.civilization_id = str(data["civilization_id"])
        return token

    def remove(self, token_id: str) -> None:
        token = self.must_get(token_id)
        del self._by_id[token.id]
        self._order.remove(token.id)

    # --------------------------------------------------------------- search

    def find_by_name(self, name: str, kind: str | None = None) -> VisualToken | None:
        target = _norm(name)
        for token in self.all():
            if kind and token.kind != kind:
                continue
            if _norm(token.name) == target:
                return token
        return None

    def search(self, query: str = "", kind: str | None = None,
               tag: str | None = None, limit: int = 20) -> list[VisualToken]:
        """Deterministic substring search over names, descriptors and tags."""
        q = _norm(query)
        hits = []
        for token in self.all():
            if kind and token.kind != kind:
                continue
            if tag and tag.lower() not in (t.lower() for t in token.tags):
                continue
            if q and q not in (_norm(token.name) + " " + _norm(token.descriptor)
                               + " " + _norm(" ".join(token.tags))):
                continue
            hits.append(token)
        return hits[:max(1, int(limit))]

    def related_to(self, token: VisualToken, max_count: int = 3) -> list[VisualToken]:
        """Tokens sharing tags or cross-links — consistency context (§13 st.20).

        A portrait of Cleopatra should reuse the *same* crown/painting/place
        tokens that exist in the world; this returns them deterministically
         ordered by shared-tag count, then by insertion order.
        """
        scored = []
        mine = {_norm(t) for t in token.tags}
        my_refs = set(token.refs.values())
        for other in self.all():
            if other.id == token.id:
                continue
            shared = len(mine & {_norm(t) for t in other.tags})
            linked = bool(my_refs & set(other.refs.values()))
            if shared or linked:
                scored.append((-(shared + int(linked)), self._order.index(other.id), other))
        scored.sort(key=lambda s: (s[0], s[1]))
        return [s[2] for s in scored[:max_count]]

    # ------------------------------------------------------------ prompting

    def prompt_fragment(self, token: VisualToken, max_count: int = 3) -> str:
        """Stable text block injected into a generation prompt for one token.

        Includes the token's own descriptor and the descriptors of related
        tokens (so a scene with two known persons renders both faces
        consistently). Depends only on registry content — never on
        wall-clock time (§20.4).
        """
        parts = []
        if token.descriptor:
            parts.append(f"{token.kind}: {token.name} — {token.descriptor}")
        elif token.name:
            parts.append(f"{token.kind}: {token.name}")
        for rel in self.related_to(token, max_count=max_count):
            if rel.descriptor:
                parts.append(f"consistent {rel.kind} «{rel.name}»: {rel.descriptor}")
        return "; ".join(parts)

    def condition_for(self, token_ids: list[str]) -> dict:
        """Aggregate conditioning payload handed to a local renderer worker.

        ``references`` lists server-local asset paths (never URLs — §20.7);
        ``negative`` collects per-token negative hints so unwanted features
        stay out of every render of that subject.
        """
        fragments, references, negative = [], [], []
        resolved = []
        for tid in token_ids:
            token = self.get(tid)
            if token is None:
                continue
            resolved.append(token)
            frag = self.prompt_fragment(token)
            if frag:
                fragments.append(frag)
            if token.asset_ref:
                references.append(token.asset_ref)
            neg = token.metadata.get("negative")
            if neg:
                negative.append(str(neg))
        return {
            "tokens": [t.to_json() for t in resolved],
            "prompt_fragments": fragments,
            "references": references,
            "negative": negative,
        }
