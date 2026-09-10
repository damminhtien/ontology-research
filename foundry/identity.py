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

import abc
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
class IdentityRecord:
    """Mutable registry state for one canonical entity (owned by a store)."""

    entity_type: str
    aliases: set[str] = field(default_factory=set)
    external_ids: dict[str, set[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class MergeOutcome:
    """Bindings moved from a duplicate entity onto its survivor."""

    moved_aliases: tuple[str, ...]
    moved_external_ids: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SplitOutcome:
    """Result of undoing one merge.

    ``restored_*`` bindings moved back onto the duplicate; ``retained_*``
    bindings that a third entity claimed after the merge and therefore stay
    where they are (multimap semantics make that visible, not silent).
    """

    restored_aliases: tuple[str, ...]
    restored_external_ids: tuple[tuple[str, str], ...]
    retained_aliases: tuple[str, ...]
    retained_external_ids: tuple[tuple[str, str], ...]


class IdentityStore(abc.ABC):
    """Persistence boundary for registry state (docs/architecture.md §4.5).

    Owns canonical-entity records, the alias/external-id multimaps, the
    token blocking index and merge redirects. :class:`IdentityService`
    keeps every policy decision (lookup order, ambiguity and review
    routing, merge/split validation) so a SQLite or graph backend can be
    substituted without touching resolution logic.
    """

    @abc.abstractmethod
    def upsert(self, entity_id: str, entity_type: str) -> None:
        """Create an empty record for ``entity_id`` when absent."""

    @abc.abstractmethod
    def get(self, entity_id: str) -> IdentityRecord | None:
        """Return the live record for ``entity_id``, or ``None``."""

    @abc.abstractmethod
    def entity_ids(self) -> Iterable[str]:
        """All recorded canonical ids (including merged-away ones)."""

    @abc.abstractmethod
    def bind_alias(self, entity_id: str, alias: str) -> None:
        """Record ``alias`` for ``entity_id`` and update the indexes."""

    @abc.abstractmethod
    def unbind_alias(self, alias: str, entity_id: str) -> None:
        """Drop one alias binding (index + record) when present."""

    @abc.abstractmethod
    def alias_owners(self, alias_norm: str) -> set[str]:
        """Canonical ids claiming the normalized alias."""

    @abc.abstractmethod
    def bind_external(self, entity_id: str, source: str, external_id: str) -> None:
        """Record an external-id binding and update the index."""

    @abc.abstractmethod
    def unbind_external(self, source: str, external_id: str, entity_id: str) -> None:
        """Drop one external-id binding (index + record) when present."""

    @abc.abstractmethod
    def external_owners(self, source: str, external_id: str) -> set[str]:
        """Canonical ids claiming ``source::external_id``."""

    @abc.abstractmethod
    def detach_bindings(
        self, entity_id: str
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        """Remove and return every binding of ``entity_id`` (merge path)."""

    @abc.abstractmethod
    def candidate_aliases(self, tokens: set[str]) -> dict[str, int]:
        """Normalized aliases sharing >= 1 token, mapped to shared-token counts."""

    @abc.abstractmethod
    def record_merge(self, duplicate_id: str, survivor_id: str) -> None:
        """Record a permanent ``duplicate_id -> survivor_id`` redirect."""

    @abc.abstractmethod
    def clear_merge(self, duplicate_id: str) -> None:
        """Remove the redirect for ``duplicate_id`` (split path)."""

    @abc.abstractmethod
    def merge_redirects(self) -> Mapping[str, str]:
        """All duplicate -> survivor redirects."""


class InMemoryIdentityStore(IdentityStore):
    """Default backend: plain dicts, rebuilt from the log on restart."""

    def __init__(self) -> None:
        """Start with an empty registry; ``rebuild_identity`` fills it."""
        self._records: dict[str, IdentityRecord] = {}
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
        # duplicate id -> surviving id, recorded by record_merge.
        self._merged_into: dict[str, str] = {}

    def upsert(self, entity_id: str, entity_type: str) -> None:
        """Create an empty record when absent (existing records untouched)."""
        if entity_id not in self._records:
            self._records[entity_id] = IdentityRecord(entity_type=entity_type)

    def get(self, entity_id: str) -> IdentityRecord | None:
        """Return the live record, or ``None`` for unknown ids."""
        return self._records.get(entity_id)

    def entity_ids(self) -> Iterable[str]:
        """All recorded canonical ids (including merged-away ones)."""
        return self._records.keys()

    def bind_alias(self, entity_id: str, alias: str) -> None:
        """Add the alias to the record and the normalized + token indexes."""
        norm = normalize_name(alias)
        if not norm:
            return  # nothing matchable after normalization
        owners = self._by_alias.setdefault(norm, set())
        if entity_id not in owners:
            for token in norm.split():
                self._token_to_norms.setdefault(token, set()).add(norm)
        owners.add(entity_id)
        self._records[entity_id].aliases.add(alias)

    def unbind_alias(self, alias: str, entity_id: str) -> None:
        """Drop one alias binding from index and record; prune empty keys."""
        norm = normalize_name(alias)
        owners = self._by_alias.get(norm)
        if owners is None or entity_id not in owners:
            return
        owners.discard(entity_id)
        self._records[entity_id].aliases.discard(alias)
        if not owners:
            del self._by_alias[norm]
            for token in norm.split():
                bucket = self._token_to_norms.get(token)
                if bucket is not None:
                    bucket.discard(norm)
                    if not bucket:
                        del self._token_to_norms[token]

    def alias_owners(self, alias_norm: str) -> set[str]:
        """Canonical ids claiming the normalized alias (empty when none)."""
        return self._by_alias.get(alias_norm, set())

    def bind_external(self, entity_id: str, source: str, external_id: str) -> None:
        """Add the external id to the record and the ``source::id`` index."""
        key = f"{source}::{external_id}"
        self._by_external.setdefault(key, set()).add(entity_id)
        self._records[entity_id].external_ids.setdefault(source, set()).add(external_id)

    def unbind_external(self, source: str, external_id: str, entity_id: str) -> None:
        """Drop one external-id binding from index and record."""
        key = f"{source}::{external_id}"
        owners = self._by_external.get(key)
        if owners is not None:
            owners.discard(entity_id)
            if not owners:
                del self._by_external[key]
        exts = self._records[entity_id].external_ids.get(source)
        if exts is not None:
            exts.discard(external_id)
            if not exts:
                del self._records[entity_id].external_ids[source]

    def external_owners(self, source: str, external_id: str) -> set[str]:
        """Canonical ids claiming ``source::external_id`` (empty when none)."""
        return self._by_external.get(f"{source}::{external_id}", set())

    def detach_bindings(
        self, entity_id: str
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        """Remove and return every binding of ``entity_id`` (merge path)."""
        record = self._records[entity_id]
        moved_aliases = tuple(sorted(record.aliases))
        moved_external = tuple(
            sorted((source, ext) for source, exts in record.external_ids.items() for ext in exts)
        )
        for alias in moved_aliases:
            self.unbind_alias(alias, entity_id)
        for source, ext in moved_external:
            self._by_external.pop(f"{source}::{ext}", None)
        record.aliases.clear()
        record.external_ids.clear()
        return moved_aliases, moved_external

    def candidate_aliases(self, tokens: set[str]) -> dict[str, int]:
        """Normalized aliases sharing >= 1 token, mapped to shared-token counts."""
        shared: dict[str, int] = {}
        for token in tokens:
            for alias_norm in self._token_to_norms.get(token, ()):
                shared[alias_norm] = shared.get(alias_norm, 0) + 1
        return shared

    def record_merge(self, duplicate_id: str, survivor_id: str) -> None:
        """Record a permanent ``duplicate_id -> survivor_id`` redirect."""
        self._merged_into[duplicate_id] = survivor_id

    def clear_merge(self, duplicate_id: str) -> None:
        """Remove the redirect for ``duplicate_id`` (split path)."""
        self._merged_into.pop(duplicate_id, None)

    def merge_redirects(self) -> Mapping[str, str]:
        """All duplicate -> survivor redirects."""
        return self._merged_into


class IdentityService:
    """Identity registry with deterministic resolution rules.

    Resolution *policy* lives here (lookup order, ambiguity and review
    routing, merge/split validation); registry *state* lives behind the
    :class:`IdentityStore` boundary. The default backend is in-memory,
    rebuilt from the log via ``foundry.merge.rebuild_identity``; a
    SQLite/graph backend swaps in without changing this class
    (docs/architecture.md §4.5).
    """

    def __init__(self, store: IdentityStore | None = None) -> None:
        """Use ``store`` for registry state; default is in-memory."""
        self._store: IdentityStore = store if store is not None else InMemoryIdentityStore()

    @property
    def store(self) -> IdentityStore:
        """The backing store (for parity checks and tooling)."""
        return self._store

    def __len__(self) -> int:
        """Number of live canonical entities (merged-away ids excluded)."""
        return len(set(self._store.entity_ids()) - set(self._store.merge_redirects()))

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
        record = self._store.get(entity_id)
        if record is None:
            self._store.upsert(entity_id, entity_type)
        elif record.entity_type != entity_type:
            raise ValueError(
                f"type conflict for {entity_id}: {record.entity_type!r} vs {entity_type!r}"
            )
        for alias in aliases or []:
            self._store.bind_alias(entity_id, alias)
        for source, value in (external_ids or {}).items():
            ids = [value] if isinstance(value, str) else value
            for ext in ids:
                self._store.bind_external(entity_id, source, ext)

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
            return {
                owner
                for owner in owners
                if (record := self._store.get(owner)) is not None
                and record.entity_type == entity_type
            }

        if not name and not external_id:
            raise ValueError("lookup() requires a name or an external_id")

        if external_id is not None:
            owners = typed_owners(self._store.external_owners(external_source or "*", external_id))
            if len(owners) == 1:
                return LookupResult(next(iter(owners)), "external_id")
            if len(owners) > 1:
                return LookupResult("", "ambiguous", tuple(sorted(owners)))

        if name:
            owners = typed_owners(self._store.alias_owners(normalize_name(name)))
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
            self._store.bind_alias(canonical_id, name)
        if external_id is not None:
            self._store.bind_external(canonical_id, external_source or "*", external_id)
        return Resolution(canonical_id, 1.0, "new", True)

    def add_external_id(self, entity_id: str, source: str, external_id: str) -> None:
        """Attach an external identifier to a known canonical entity."""
        if self._store.get(entity_id) is None:
            raise ValueError(f"unknown canonical id {entity_id}")
        self._store.bind_external(entity_id, source, external_id)

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
        duplicate = self._store.get(duplicate_id)
        survivor = self._store.get(survivor_id)
        if duplicate is None:
            raise ValueError(f"unknown canonical id {duplicate_id}")
        if survivor is None:
            raise ValueError(f"unknown canonical id {survivor_id}")
        if duplicate_id in self._store.merge_redirects():
            raise ValueError(f"{duplicate_id} has already been merged")
        if duplicate.entity_type != survivor.entity_type:
            raise ValueError(
                f"type conflict on merge: {survivor.entity_type!r} vs {duplicate.entity_type!r}"
            )

        moved_aliases, moved_external_ids = self._store.detach_bindings(duplicate_id)
        for alias in moved_aliases:
            self._store.bind_alias(survivor_id, alias)
        for source, ext in moved_external_ids:
            self._store.bind_external(survivor_id, source, ext)
        self._store.record_merge(duplicate_id, survivor_id)
        return MergeOutcome(
            moved_aliases=tuple(moved_aliases),
            moved_external_ids=tuple(moved_external_ids),
        )

    def merged_into(self, entity_id: str) -> str:
        """Return the final survivor for a merged id, or '' when not merged."""
        redirects = self._store.merge_redirects()
        seen = {entity_id}
        current = entity_id
        while current in redirects:
            current = redirects[current]
            if current in seen:
                break  # defensive; cycles cannot be constructed via merge_entities
            seen.add(current)
        return current if current != entity_id else ""

    def split_entity(
        self,
        survivor_id: str,
        duplicate_id: str,
        moved_aliases: tuple[str, ...] | list[str],
        moved_external_ids: tuple[tuple[str, str], ...] | list[tuple[str, str]],
    ) -> SplitOutcome:
        """Undo one merge: move the recorded bindings back to ``duplicate_id``.

        The restore set comes from the ``EntityMerged`` event payload (see
        ``foundry.merge``), so replaying merge→split reproduces the exact
        pre-merge registry state. Bindings that a third entity claimed after
        the merge are *retained* where they are and reported, never silently
        stolen back. The duplicate's location history stays merged into the
        survivor — history re-partition belongs to the assertion model
        (``docs/architecture.md`` §4.2), where each assertion names its
        subject explicitly.

        Raises:
            ValueError: On unknown ids, a duplicate that is not currently
                merged into this survivor, or an entity-type conflict.
        """
        duplicate = self._store.get(duplicate_id)
        survivor = self._store.get(survivor_id)
        if duplicate is None:
            raise ValueError(f"unknown canonical id {duplicate_id}")
        if survivor is None:
            raise ValueError(f"unknown canonical id {survivor_id}")
        if self._store.merge_redirects().get(duplicate_id) != survivor_id:
            raise ValueError(f"{duplicate_id} has not been merged into {survivor_id}; cannot split")
        if duplicate.entity_type != survivor.entity_type:
            raise ValueError(
                f"type conflict on split: {survivor.entity_type!r} vs {duplicate.entity_type!r}"
            )

        restored_aliases: list[str] = []
        retained_aliases: list[str] = []
        restored_external: list[tuple[str, str]] = []
        retained_external: list[tuple[str, str]] = []

        for alias in moved_aliases:
            if survivor_id in self._store.alias_owners(normalize_name(alias)):
                self._store.unbind_alias(alias, survivor_id)
                self._store.bind_alias(duplicate_id, alias)
                restored_aliases.append(alias)
            else:
                retained_aliases.append(alias)

        for source, ext in moved_external_ids:
            if survivor_id in self._store.external_owners(source, ext):
                self._store.unbind_external(source, ext, survivor_id)
                self._store.bind_external(duplicate_id, source, ext)
                restored_external.append((source, ext))
            else:
                retained_external.append((source, ext))

        self._store.clear_merge(duplicate_id)
        return SplitOutcome(
            restored_aliases=tuple(sorted(restored_aliases)),
            restored_external_ids=tuple(sorted(restored_external)),
            retained_aliases=tuple(sorted(retained_aliases)),
            retained_external_ids=tuple(sorted(retained_external)),
        )

    def knows(self, entity_id: str) -> bool:
        """True when the registry holds a record for this canonical id."""
        return self._store.get(entity_id) is not None

    def identity(self, entity_id: str) -> tuple[str, frozenset[str], dict[str, frozenset[str]]]:
        """Return (entity_type, aliases, external_ids) for a canonical id."""
        record = self._store.get(entity_id)
        if record is None:
            raise KeyError(entity_id)
        return (
            record.entity_type,
            frozenset(record.aliases),
            {source: frozenset(ids) for source, ids in record.external_ids.items()},
        )

    # -- internals ---------------------------------------------------------

    def _fuzzy_candidates(
        self, norm: str, entity_type: str | None = None
    ) -> list[tuple[str, float]]:
        """Return candidate (canonical_id, score) pairs above the review threshold.

        Scoring uses the overlap coefficient |A intersect B| / min(|A|, |B|),
        which suits subset-style name variants. Candidate generation is blocked
        through the store's token index: only aliases sharing at least one
        token with the query can score above zero, so the index prunes without
        changing results. An alias bound to several entities contributes each
        owner (best score wins per owner); owners of a different
        ``entity_type`` are filtered out. Results are proposals only: callers
        must route them to review instead of auto-merging.
        """
        tokens = set(norm.split())
        if not tokens:
            return []
        best: dict[str, float] = {}
        for alias_norm, overlap in self._store.candidate_aliases(tokens).items():
            score = overlap / min(len(tokens), len(alias_norm.split()))
            if score < REVIEW_THRESHOLD:
                continue
            for owner in self._store.alias_owners(alias_norm):
                record = self._store.get(owner)
                if (
                    entity_type is not None
                    and record is not None
                    and record.entity_type != entity_type
                ):
                    continue
                if score > best.get(owner, 0.0):
                    best[owner] = score
        return sorted(best.items())
