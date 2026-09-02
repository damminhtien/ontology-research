"""Entity identity resolution.

Ontology does not solve identity: "USS Gerald R. Ford", "CVN-78" and
"Gerald Ford Carrier" must all collapse to one canonical id. This module
implements a precision-first resolution service:

1. external id hit      -> auto-resolved, confidence 1.00 (method=external_id)
2. exact alias match    -> auto-resolved, confidence 0.95 (method=alias)
3. fuzzy token overlap  -> NEVER auto-merged; returned as method=``review``
   with the ranked candidate canonical ids so a human (or an upstream policy)
   can confirm the merge. Lexical similarity alone cannot distinguish a
   genuine variant ("Gerald R. Ford Carrier") from a different unit
   ("Alpha Patrol Unit Two"), so recall here must never bypass review.
   Exception (ADR-0006): when the caller supplies an ``external_id`` and it
   misses, the external id itself asserts identity, so fuzzy candidates do
   not force review — a new canonical entity is created instead.

Contract properties:

* **Pure lookup**: :meth:`IdentityService.lookup` answers questions without
  mutating anything; only :meth:`IdentityService.resolve` mints entities.
* **Alias multimap**: an alias may be bound to several canonical entities.
  Collisions surface as ``ambiguous`` lookups routed to review instead of
  being silently resolved to the first registrant.
* **Type-aware**: bindings owned by a different ``entity_type`` are ignored
  during lookup — the same surface name may legitimately denote different
  entities across types.
* **Multi-valued identifiers**: an entity may hold several external ids per
  source (registry re-assignments, cross-walks).
* **Persistent**: the registry is a projection of the event log — every
  mutation performed by ``resolve``/``register`` is mirrored by an
  ``EntityCreated``/``EntityMerged`` event, so the full state can be rebuilt
  from the log (see ``foundry.merge.rebuild_identity``).

Canonical ids use ``urn:world:entity:<uuid>`` - never database auto-increment.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from foundry.namespaces import new_entity_id

CONFIDENCE_EXTERNAL_ID = 1.0
CONFIDENCE_ALIAS = 0.95
REVIEW_THRESHOLD = 0.50

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_name(text: str) -> str:
    """Normalize a name for matching: casefold, strip punctuation, squeeze spaces."""
    cleaned = _NON_WORD.sub(" ", text.casefold())
    return " ".join(cleaned.split())


@dataclass(frozen=True)
class LookupResult:
    """Outcome of a pure (non-mutating) registry lookup.

    Attributes:
        canonical_id: Unique matching canonical id; ``""`` when none or
            ambiguous.
        method: ``external_id`` (trusted identifier hit), ``alias`` (exact
            normalized-name hit), ``ambiguous`` (several same-type owners for
            the requested key), or ``miss``.
        candidates: All matching canonical ids for ``ambiguous`` outcomes.
    """

    canonical_id: str
    method: str
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving one reference to a canonical identity.

    Attributes:
        canonical_id: The resolved (or freshly minted) canonical identifier;
            empty for ``review`` outcomes where no merge may be assumed.
        confidence: Score in [0, 1]; low values mean "needs review".
        method: One of ``external_id``, ``alias``, ``new``, ``review``.
        is_new: True when a new canonical entity was created by this call.
        candidates: Proposed canonical ids for ``review`` outcomes.
    """

    canonical_id: str
    confidence: float
    method: str
    is_new: bool
    candidates: tuple[str, ...] = ()


@dataclass
class _Record:
    entity_type: str
    aliases: set[str] = field(default_factory=set)
    external_ids: dict[str, set[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class MergeOutcome:
    """Bindings moved from a duplicate entity onto its survivor."""

    moved_aliases: tuple[str, ...]
    moved_external_ids: tuple[tuple[str, str], ...]


class IdentityService:
    """In-memory identity registry with deterministic resolution rules.

    Sufficient for Phase 2 correctness work; persistence and serving scale
    are deferred to the platform layer without changing the contract.
    """

    def __init__(self) -> None:
        """Start with an empty in-memory registry."""
        self._records: dict[str, _Record] = {}
        # Alias multimap: normalized alias -> canonical ids claiming it.
        self._by_alias: dict[str, set[str]] = {}
        # External-id multimap: "source::id" -> canonical ids claiming it.
        self._by_external: dict[str, set[str]] = {}
        # Blocking index: token -> normalized aliases containing it. Candidate
        # generation for fuzzy matching only visits aliases sharing at least
        # one token with the query; a zero-token-overlap pair always scores 0
        # under the overlap coefficient, so blocking is exact (no false
        # negatives) and turns the O(aliases) scan into O(shared candidates).
        self._token_to_norms: dict[str, set[str]] = {}
        # duplicate id -> surviving id, recorded by merge_entities.
        self._merged_into: dict[str, str] = {}

    def __len__(self) -> int:
        """Number of live canonical entities (merged-away ids excluded)."""
        return sum(1 for rid in self._records if rid not in self._merged_into)

    def register(
        self,
        *,
        entity_id: str,
        entity_type: str,
        aliases: list[str] | None = None,
        external_ids: Mapping[str, str | Iterable[str]] | None = None,
    ) -> None:
        """Register or enrich an existing canonical entity.

        Raises:
            ValueError: On a type conflict for a known id.
        """
        record = self._records.get(entity_id)
        if record is None:
            record = _Record(entity_type=entity_type)
            self._records[entity_id] = record
        elif record.entity_type != entity_type:
            raise ValueError(
                f"type conflict for {entity_id}: {record.entity_type!r} vs {entity_type!r}"
            )
        for alias in aliases or []:
            self._bind_alias(entity_id, alias)
        for source, value in (external_ids or {}).items():
            ids = [value] if isinstance(value, str) else value
            for ext in ids:
                self._bind_external(entity_id, source, ext)

    def lookup(
        self,
        *,
        name: str | None = None,
        external_source: str | None = None,
        external_id: str | None = None,
        entity_type: str | None = None,
    ) -> LookupResult:
        """Pure registry lookup: never mutates state and never mints ids.

        Lookup order: trusted external id first, then exact normalized alias.
        Type-aware: when ``entity_type`` is given, bindings owned by a
        different type are ignored — the same surface name may legitimately
        denote different entities across types. An exact key held by several
        same-type entities is ``ambiguous``: collisions surface here instead
        of silently resolving to the first registrant.

        Raises:
            ValueError: If neither ``name`` nor ``external_id`` is provided.
        """

        def typed_owners(owners: set[str]) -> set[str]:
            if entity_type is None:
                return owners
            return {owner for owner in owners if self._records[owner].entity_type == entity_type}

        if not name and not external_id:
            raise ValueError("lookup() requires a name or an external_id")

        if external_id is not None:
            key = f"{external_source or '*'}::{external_id}"
            owners = typed_owners(self._by_external.get(key, set()))
            if len(owners) == 1:
                return LookupResult(next(iter(owners)), "external_id")
            if len(owners) > 1:
                return LookupResult("", "ambiguous", tuple(sorted(owners)))

        if name:
            owners = typed_owners(self._by_alias.get(normalize_name(name), set()))
            if len(owners) == 1:
                return LookupResult(next(iter(owners)), "alias")
            if len(owners) > 1:
                return LookupResult("", "ambiguous", tuple(sorted(owners)))

        return LookupResult("", "miss")

    def resolve(
        self,
        *,
        name: str | None = None,
        external_source: str | None = None,
        external_id: str | None = None,
        entity_type: str,
    ) -> Resolution:
        """Resolve one reference to a canonical identity, creating one if needed.

        Pure lookup first (:meth:`lookup`): trusted external id, then exact
        alias — type-aware, ambiguity-aware. On a miss, fuzzy token overlap
        proposes review candidates (never auto-merged) unless the caller
        supplied an external id — ADR-0006: the external id itself asserts
        identity, so the reference mints a new canonical entity instead.
        Fuzzy review applies only when no external id is given.

        Raises:
            ValueError: If neither ``name`` nor ``external_id`` is provided.
        """
        if not name and not external_id:
            raise ValueError("resolve() requires a name or an external_id")

        found = self.lookup(
            name=name,
            external_source=external_source,
            external_id=external_id,
            entity_type=entity_type,
        )
        if found.method == "external_id":
            return Resolution(found.canonical_id, CONFIDENCE_EXTERNAL_ID, "external_id", False)
        if found.method == "alias":
            return Resolution(found.canonical_id, CONFIDENCE_ALIAS, "alias", False)
        if found.method == "ambiguous":
            # Exact-key collision between same-type entities: a data problem,
            # stronger evidence than fuzzy similarity — route to review.
            confidence = CONFIDENCE_EXTERNAL_ID if external_id is not None else CONFIDENCE_ALIAS
            return Resolution("", confidence, "review", False, found.candidates)

        if name:
            candidates = self._fuzzy_candidates(normalize_name(name), entity_type)
            if candidates and external_id is None:
                # No externally asserted identity: fuzzy similarity is the only
                # evidence, so route to review instead of guessing (ADR-0003).
                best = max(score for _, score in candidates)
                return Resolution(
                    "",
                    round(best, 4),
                    "review",
                    False,
                    tuple(sorted(cid for cid, _ in candidates)),
                )

        canonical_id = new_entity_id()
        self.register(entity_id=canonical_id, entity_type=entity_type)
        if name:
            self._bind_alias(canonical_id, name)
        if external_id is not None:
            self._bind_external(canonical_id, external_source or "*", external_id)
        return Resolution(canonical_id, 1.0, "new", True)

    def add_external_id(self, entity_id: str, source: str, external_id: str) -> None:
        """Attach an external identifier to a known canonical entity."""
        if entity_id not in self._records:
            raise ValueError(f"unknown canonical id {entity_id}")
        self._bind_external(entity_id, source, external_id)

    def merge_entities(self, survivor_id: str, duplicate_id: str) -> MergeOutcome:
        """Collapse ``duplicate_id`` into ``survivor_id`` (under-merge repair).

        Moves every alias and external-id binding owned by the duplicate to the
        survivor, so future exact lookups resolve to the survivor, and records
        a permanent redirect. The duplicate keeps its (now empty) record so its
        type stays introspectable but owns nothing of its own. Callers must
        persist the merge as an ``EntityMerged`` event so replays reproduce it.

        Returns:
            The bindings that were moved (for the event payload).

        Raises:
            ValueError: On self-merge, unknown ids, an already-merged
                duplicate, or an entity-type conflict.
        """
        if survivor_id == duplicate_id:
            raise ValueError("cannot merge an entity into itself")
        duplicate = self._records.get(duplicate_id)
        survivor = self._records.get(survivor_id)
        if duplicate is None:
            raise ValueError(f"unknown canonical id {duplicate_id}")
        if survivor is None:
            raise ValueError(f"unknown canonical id {survivor_id}")
        if duplicate_id in self._merged_into:
            raise ValueError(f"{duplicate_id} has already been merged")
        if duplicate.entity_type != survivor.entity_type:
            raise ValueError(
                f"type conflict on merge: {survivor.entity_type!r} vs {duplicate.entity_type!r}"
            )

        moved_aliases = sorted(duplicate.aliases)
        moved_external_ids = sorted(
            (source, ext) for source, exts in duplicate.external_ids.items() for ext in exts
        )
        for alias in moved_aliases:
            self._unbind_alias(normalize_name(alias), duplicate_id)
        for source, ext in moved_external_ids:
            self._by_external.pop(f"{source}::{ext}", None)
        duplicate.aliases.clear()
        duplicate.external_ids.clear()
        for alias in moved_aliases:
            self._bind_alias(survivor_id, alias)
        for source, ext in moved_external_ids:
            self._bind_external(survivor_id, source, ext)
        self._merged_into[duplicate_id] = survivor_id
        return MergeOutcome(
            moved_aliases=tuple(moved_aliases),
            moved_external_ids=tuple(moved_external_ids),
        )

    def merged_into(self, entity_id: str) -> str:
        """Return the final survivor for a merged id, or '' when not merged."""
        seen = {entity_id}
        current = entity_id
        while current in self._merged_into:
            current = self._merged_into[current]
            if current in seen:
                break  # defensive; cycles cannot be constructed via merge_entities
            seen.add(current)
        return current if current != entity_id else ""

    def identity(self, entity_id: str) -> tuple[str, frozenset[str], dict[str, frozenset[str]]]:
        """Return (entity_type, aliases, external_ids) for a canonical id."""
        record = self._records[entity_id]
        return (
            record.entity_type,
            frozenset(record.aliases),
            {source: frozenset(ids) for source, ids in record.external_ids.items()},
        )

    # -- internals ---------------------------------------------------------

    def _unbind_alias(self, norm: str, owner: str) -> None:
        """Remove an alias binding (merge-only; ingestion never unbinds)."""
        owners = self._by_alias.get(norm)
        if owners is None or owner not in owners:
            return
        owners.discard(owner)
        if owners:
            return
        del self._by_alias[norm]
        for token in norm.split():
            bucket = self._token_to_norms.get(token)
            if bucket is not None:
                bucket.discard(norm)
                if not bucket:
                    del self._token_to_norms[token]

    def _bind_alias(self, entity_id: str, alias: str) -> None:
        norm = normalize_name(alias)
        if not norm:
            return  # nothing matchable after normalization
        owners = self._by_alias.setdefault(norm, set())
        if entity_id not in owners:
            for token in norm.split():
                self._token_to_norms.setdefault(token, set()).add(norm)
        owners.add(entity_id)
        self._records[entity_id].aliases.add(alias)

    def _bind_external(self, entity_id: str, source: str, external_id: str) -> None:
        key = f"{source}::{external_id}"
        self._by_external.setdefault(key, set()).add(entity_id)
        self._records[entity_id].external_ids.setdefault(source, set()).add(external_id)

    def _fuzzy_candidates(
        self, norm: str, entity_type: str | None = None
    ) -> list[tuple[str, float]]:
        """Return candidate (canonical_id, score) pairs above the review threshold.

        Scoring uses the overlap coefficient |A intersect B| / min(|A|, |B|),
        which suits subset-style name variants. Candidate generation is blocked
        through the token index: only aliases sharing at least one token with
        the query can score above zero, so the index prunes without changing
        results. An alias bound to several entities contributes each owner
        (best score wins per owner); owners of a different ``entity_type`` are
        filtered out. Results are proposals only: callers must route them to
        review instead of auto-merging.
        """
        tokens = set(norm.split())
        if not tokens:
            return []
        shared: dict[str, int] = {}
        for token in tokens:
            for alias_norm in self._token_to_norms.get(token, ()):
                shared[alias_norm] = shared.get(alias_norm, 0) + 1
        best: dict[str, float] = {}
        for alias_norm, overlap in shared.items():
            score = overlap / min(len(tokens), len(alias_norm.split()))
            if score < REVIEW_THRESHOLD:
                continue
            for owner in self._by_alias[alias_norm]:
                if entity_type is not None and self._records[owner].entity_type != entity_type:
                    continue
                if score > best.get(owner, 0.0):
                    best[owner] = score
        return sorted(best.items())
