"""Identity resolution with explicit source-record and canonical identities.

Names and identifiers generate candidates. Only a registered, unique
authoritative identifier, an existing source-record key, or explicit
same-entity evidence can produce a match. A new identifier is never attached
to an existing entity because its name happens to match.

Contract properties:

* **Pure lookup**: :meth:`IdentityService.lookup` answers questions without
  mutating anything; only :meth:`IdentityService.resolve` mints entities.
* **Alias multimap**: an alias may be bound to several canonical entities.
  Collisions surface as ``ambiguous`` lookups routed to review instead of
  being silently resolved to the first registrant.
* **Type-aware**: name candidates owned by a different ``entity_type`` are
  ignored. A trusted identifier whose owner has a conflicting type is a
  conflict, not a miss.
* **Multi-valued identifiers**: an entity may hold several external ids per
  source. Each namespace has an explicit uniqueness policy; unknown namespaces
  are candidate-only.
* **Replayable source records**: a source record key is namespaced by its
  source and maps to a canonical id independently of that canonical id.
* **Persistent**: the registry is a projection of the event log — every
  mutation performed by ``resolve``/``register`` is mirrored by an
  ``EntityCreated``/``EntityMerged`` event, so the full state can be rebuilt
  from the log (see ``foundry.merge.rebuild_identity``).

Canonical ids use ``urn:world:entity:<uuid>`` - never database auto-increment.
"""

from __future__ import annotations

import abc
import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps
from typing import Concatenate

from foundry.namespaces import new_entity_id
from foundry.provenance import normalize_source_id

CONFIDENCE_EXTERNAL_ID = 1.0
CONFIDENCE_ALIAS = 0.40
REVIEW_THRESHOLD = 0.50

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def _atomic_identity_mutation[**P, R](
    method: Callable[Concatenate[IdentityService, P], R],
) -> Callable[Concatenate[IdentityService, P], R]:
    """Run one policy mutation under the backing store's transaction."""

    @wraps(method)
    def wrapped(self: IdentityService, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._store.transaction():
            return method(self, *args, **kwargs)

    return wrapped


def normalize_name(text: str) -> str:
    """Normalize a name for matching: casefold, strip punctuation, squeeze spaces."""
    cleaned = _NON_WORD.sub(" ", text.casefold())
    return " ".join(cleaned.split())


class IdentityDecision(StrEnum):
    """Decision made by the resolver, separate from candidate generation."""

    MATCH = "MATCH"
    NEW = "NEW"
    REVIEW = "REVIEW"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class IdentifierPolicy:
    """Confidence policy for one identifier namespace."""

    unique_authoritative: bool
    confidence: float

    def __post_init__(self) -> None:
        """Keep namespace confidence values inside the review scale."""
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("identifier confidence must be in [0, 1]")


# Only namespaces with defined uniqueness semantics may auto-match. Every
# unlisted namespace defaults to candidate-only, including reused identifiers
# such as MMSI and generic registry numbers.
DEFAULT_IDENTIFIER_POLICIES: Mapping[str, IdentifierPolicy] = {
    "wikidata": IdentifierPolicy(unique_authoritative=True, confidence=1.0),
    "gleif-lei": IdentifierPolicy(unique_authoritative=True, confidence=1.0),
    "usn-hull": IdentifierPolicy(unique_authoritative=True, confidence=1.0),
    "sensor-registry": IdentifierPolicy(unique_authoritative=True, confidence=1.0),
    # ICAO assigns exclusive 24-bit addresses to aircraft. All ADS-B feeds
    # share this identifier, so provider-specific namespaces would split one
    # aircraft into separate canonical entities. Reassignment is exceptional
    # and can be corrected with the replayable split workflow if evidenced.
    "icao24": IdentifierPolicy(unique_authoritative=True, confidence=1.0),
    "ais-mmsi": IdentifierPolicy(unique_authoritative=False, confidence=0.55),
    "mnis": IdentifierPolicy(unique_authoritative=False, confidence=0.55),
    "naval-registry": IdentifierPolicy(unique_authoritative=False, confidence=0.55),
}


@dataclass(frozen=True, order=True)
class SourceRecordIdentity:
    """Stable identity of a record inside one source, distinct from an entity."""

    source_id: str
    record_id: str

    def __post_init__(self) -> None:
        """Normalize the source namespace and reject incomplete record keys."""
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("source record identity requires source_id and record_id")
        if not isinstance(self.record_id, str) or not self.record_id.strip():
            raise ValueError("source record identity requires source_id and record_id")
        object.__setattr__(self, "source_id", normalize_source_id(self.source_id))
        object.__setattr__(self, "record_id", self.record_id.strip())

    def to_dict(self) -> dict[str, str]:
        """Serialize the stable source-local identity."""
        return {"source_id": self.source_id, "record_id": self.record_id}


@dataclass(frozen=True)
class IdentityEvidence:
    """Auditable evidence for an explicit identity correction."""

    evidence_id: str
    source: str
    basis: str
    recorded_by: str
    description: str = ""

    def __post_init__(self) -> None:
        """Require an auditable identity-evidence reference."""
        for name in ("evidence_id", "source", "basis", "recorded_by"):
            if not getattr(self, name).strip():
                raise ValueError(f"identity evidence {name} must be non-empty")

    def to_dict(self) -> dict[str, str]:
        """Serialize evidence fields for an immutable event payload."""
        return {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "basis": self.basis,
            "recorded_by": self.recorded_by,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, str]) -> IdentityEvidence:
        """Parse and validate an evidence mapping from a command payload."""
        return cls(
            evidence_id=str(value["evidence_id"]),
            source=str(value["source"]),
            basis=str(value["basis"]),
            recorded_by=str(value["recorded_by"]),
            description=str(value.get("description", "")),
        )


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
        method: Decision path: ``source_record``, ``external_id``, ``new``,
            ``review`` or ``conflict``.
        is_new: True when a new canonical entity was created by this call.
        candidates: Proposed canonical ids for ``review`` outcomes.
    """

    canonical_id: str
    confidence: float
    method: str
    is_new: bool
    candidates: tuple[str, ...] = ()
    decision: IdentityDecision = IdentityDecision.NEW
    evidence: Mapping[str, str] | None = None
    source_record_linked: bool = False
    reason: str = ""
    candidate_method: str = ""


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
    routing, merge/split validation) so a persistent backend can be
    substituted without touching resolution logic.

    ``transaction`` lets compound identity operations update durable indexes
    atomically. The in-memory implementation needs no transaction.
    """

    def transaction(self) -> AbstractContextManager[object]:
        """Return a transaction scope for compound writes."""
        return nullcontext()

    @abc.abstractmethod
    def upsert(self, entity_id: str, entity_type: str) -> None:
        """Create an empty record for ``entity_id`` when absent."""

    @abc.abstractmethod
    def get(self, entity_id: str) -> IdentityRecord | None:
        """Return the live record for ``entity_id``, or ``None``."""

    @abc.abstractmethod
    def entity_ids(self) -> Iterable[str]:
        """All recorded canonical ids (including merged-away ones)."""

    def live_entity_count(self) -> int:
        """Count active canonical entities without requiring service policy."""
        redirects = self.merge_redirects()
        return sum(1 for entity_id in self.entity_ids() if entity_id not in redirects)

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

    @abc.abstractmethod
    def source_record_owner(self, source_record: SourceRecordIdentity) -> str | None:
        """Canonical id recorded for a source-local record key, if any."""

    @abc.abstractmethod
    def bind_source_record(self, source_record: SourceRecordIdentity, entity_id: str) -> None:
        """Bind a source-local record key to one canonical id."""

    @abc.abstractmethod
    def source_records(self) -> Mapping[SourceRecordIdentity, str]:
        """All source-record to canonical-id assignments."""

    @abc.abstractmethod
    def add_cannot_link(self, left_id: str, right_id: str, evidence: IdentityEvidence) -> None:
        """Record evidence that two canonical entities must remain separate."""

    @abc.abstractmethod
    def cannot_links(self) -> Mapping[tuple[str, str], IdentityEvidence]:
        """All sorted cannot-link pairs and their evidence."""

    @abc.abstractmethod
    def cannot_link_evidence(self, left_id: str, right_id: str) -> IdentityEvidence | None:
        """Return one pair constraint without scanning the full constraint set."""

    @abc.abstractmethod
    def cannot_link_between_components(
        self, left_id: str, right_id: str
    ) -> IdentityEvidence | None:
        """Return a cannot-link constraint between two current merge components."""


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
        # A source record is not a canonical entity. The same source-local key
        # always replays to its original canonical identity; redirects are
        # followed only when resolving it, so a later split restores its owner.
        self._source_records: dict[SourceRecordIdentity, str] = {}
        self._cannot_links: dict[tuple[str, str], IdentityEvidence] = {}

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
            self.unbind_external(source, ext, entity_id)
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

    def source_record_owner(self, source_record: SourceRecordIdentity) -> str | None:
        """Return the canonical id assigned to a source-local record."""
        return self._source_records.get(source_record)

    def bind_source_record(self, source_record: SourceRecordIdentity, entity_id: str) -> None:
        """Bind a source record once; conflicting reassignment is rejected."""
        owner = self._source_records.get(source_record)
        if owner is not None and owner != entity_id:
            raise ValueError(
                f"source record {source_record.source_id}::{source_record.record_id} "
                f"is already assigned to {owner}"
            )
        self._source_records[source_record] = entity_id

    def source_records(self) -> Mapping[SourceRecordIdentity, str]:
        """All source-record assignments."""
        return self._source_records

    def add_cannot_link(self, left_id: str, right_id: str, evidence: IdentityEvidence) -> None:
        """Record a symmetric cannot-link constraint."""
        pair = tuple(sorted((left_id, right_id)))
        previous = self._cannot_links.get(pair)
        if previous is None:
            self._cannot_links[pair] = evidence

    def cannot_links(self) -> Mapping[tuple[str, str], IdentityEvidence]:
        """All sorted cannot-link constraints."""
        return self._cannot_links

    def cannot_link_evidence(self, left_id: str, right_id: str) -> IdentityEvidence | None:
        """Look up one symmetric pair constraint."""
        return self._cannot_links.get(tuple(sorted((left_id, right_id))))

    def cannot_link_between_components(
        self, left_id: str, right_id: str
    ) -> IdentityEvidence | None:
        """Find a cannot-link across current components, including merged members."""
        redirects = self._merged_into

        def members(root: str) -> set[str]:
            found = {root}
            pending = [root]
            while pending:
                current = pending.pop()
                children = [
                    duplicate for duplicate, survivor in redirects.items() if survivor == current
                ]
                for child in children:
                    if child not in found:
                        found.add(child)
                        pending.append(child)
            return found

        left_members = members(left_id)
        right_members = members(right_id)
        for (left, right), evidence in self._cannot_links.items():
            if (left in left_members and right in right_members) or (
                right in left_members and left in right_members
            ):
                return evidence
        return None


class IdentityService:
    """Identity registry with deterministic resolution rules.

    Resolution *policy* lives here (lookup order, ambiguity and review
    routing, merge/split validation); registry *state* lives behind the
    :class:`IdentityStore` boundary. The default backend is in-memory,
    rebuilt from the log via ``foundry.merge.rebuild_identity``; a
    PostgreSQL backend swaps in without changing this class
    (docs/architecture.md §4.5).
    """

    def __init__(
        self,
        store: IdentityStore | None = None,
        identifier_policies: Mapping[str, IdentifierPolicy] | None = None,
    ) -> None:
        """Use ``store`` for state and namespace-specific identifier policy."""
        self._store: IdentityStore = store if store is not None else InMemoryIdentityStore()
        self._identifier_policies = {
            name.casefold(): policy for name, policy in DEFAULT_IDENTIFIER_POLICIES.items()
        }
        self._identifier_policies.update(
            {name.casefold(): policy for name, policy in (identifier_policies or {}).items()}
        )

    @property
    def store(self) -> IdentityStore:
        """The backing store (for parity checks and tooling)."""
        return self._store

    def __len__(self) -> int:
        """Number of live canonical entities (merged-away ids excluded)."""
        return self._store.live_entity_count()

    @_atomic_identity_mutation
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

    @_atomic_identity_mutation
    def resolve(
        self,
        *,
        name: str | None = None,
        external_source: str | None = None,
        external_id: str | None = None,
        entity_type: str,
        source_record: SourceRecordIdentity | None = None,
        prepare_only: bool = False,
    ) -> Resolution:
        """Resolve a source record without treating names as identity proof.

        A previously mapped source-record key and an exact hit in a namespace
        explicitly marked unique and authoritative can return ``MATCH``.
        Names, aliases, Splink scores, and non-unique identifiers produce
        ``REVIEW`` candidates only. A miss with no candidates proposes a new
        canonical entity. ``prepare_only=True`` keeps the registry unchanged
        until a caller commits the decision to the event log.

        Raises:
            ValueError: If neither ``name`` nor ``external_id`` is provided.
        """
        if not name and not external_id:
            raise ValueError("resolve() requires a name or an external_id")

        identifier_source = external_source or "*"
        policy = self._identifier_policies.get(
            identifier_source.casefold(), IdentifierPolicy(False, 0.40)
        )

        if source_record is not None:
            source_owner = self._store.source_record_owner(source_record)
            if source_owner is not None:
                canonical_id = self.merged_into(source_owner) or source_owner
                record = self._store.get(canonical_id)
                if record is None or record.entity_type != entity_type:
                    return Resolution(
                        "",
                        0.0,
                        "source_record_conflict",
                        False,
                        (canonical_id,),
                        IdentityDecision.CONFLICT,
                        reason="source record is already assigned to a different entity type",
                    )
                if external_id is not None:
                    owners = self._canonical_owners(
                        self._store.external_owners(identifier_source, external_id)
                    )
                    if owners and owners != {canonical_id}:
                        return Resolution(
                            "",
                            policy.confidence,
                            "identifier_conflict",
                            False,
                            tuple(sorted(owners)),
                            IdentityDecision.CONFLICT,
                            reason=(
                                "source record and identifier point to different canonical entities"
                            ),
                        )
                return Resolution(
                    canonical_id,
                    1.0,
                    "source_record",
                    False,
                    (),
                    IdentityDecision.MATCH,
                    evidence={"type": "source_record", **source_record.to_dict()},
                )

        if external_id is not None and policy.unique_authoritative:
            owners = self._canonical_owners(
                self._store.external_owners(identifier_source, external_id)
            )
            if len(owners) > 1:
                return Resolution(
                    "",
                    policy.confidence,
                    "identifier_conflict",
                    False,
                    tuple(sorted(owners)),
                    IdentityDecision.CONFLICT,
                    reason=(
                        f"unique identifier {identifier_source}::{external_id} has multiple owners"
                    ),
                )
            if len(owners) == 1:
                canonical_id = next(iter(owners))
                record = self._store.get(canonical_id)
                if record is None or record.entity_type != entity_type:
                    return Resolution(
                        "",
                        policy.confidence,
                        "identifier_conflict",
                        False,
                        (canonical_id,),
                        IdentityDecision.CONFLICT,
                        reason="authoritative identifier owner has a conflicting entity type",
                    )
                if source_record is not None and not prepare_only:
                    self._store.bind_source_record(source_record, canonical_id)
                return Resolution(
                    canonical_id,
                    policy.confidence,
                    "external_id",
                    False,
                    (),
                    IdentityDecision.MATCH,
                    evidence={
                        "type": "authoritative_identifier",
                        "source": identifier_source,
                        "external_id": external_id,
                    },
                    source_record_linked=source_record is not None,
                )

        candidates: set[str] = set()
        candidate_method = ""
        confidence = policy.confidence if external_id is not None else CONFIDENCE_ALIAS
        if external_id is not None:
            identifier_owners = self._canonical_owners(
                self._store.external_owners(identifier_source, external_id)
            )
            typed_owners = {
                owner
                for owner in identifier_owners
                if (record := self._store.get(owner)) is not None
                and record.entity_type == entity_type
            }
            candidates.update(typed_owners)
            if typed_owners:
                candidate_method = "identifier_candidate"

        if name:
            exact_owners = self._canonical_owners(self._store.alias_owners(normalize_name(name)))
            exact_owners = {
                owner
                for owner in exact_owners
                if (record := self._store.get(owner)) is not None
                and record.entity_type == entity_type
            }
            if exact_owners:
                candidates.update(exact_owners)
                candidate_method = "alias_candidate"
                confidence = CONFIDENCE_ALIAS
            else:
                fuzzy = self._fuzzy_candidates(normalize_name(name), entity_type)
                if fuzzy:
                    candidates.update(canonical_id for canonical_id, _ in fuzzy)
                    candidate_method = "fuzzy_candidate"
                    confidence = max(score for _, score in fuzzy)

        new_authoritative_identifier = (
            external_id is not None
            and policy.unique_authoritative
            and not self._canonical_owners(
                self._store.external_owners(identifier_source, external_id)
            )
        )
        if candidates and not new_authoritative_identifier:
            return Resolution(
                "",
                round(confidence, 4),
                "review",
                False,
                tuple(sorted(candidates)),
                IdentityDecision.REVIEW,
                reason="candidate evidence does not establish same-entity identity",
                candidate_method=candidate_method,
            )

        canonical_id = new_entity_id()
        if not prepare_only:
            self.register(entity_id=canonical_id, entity_type=entity_type)
            if name:
                self._store.bind_alias(canonical_id, name)
            if external_id is not None:
                self._store.bind_external(canonical_id, identifier_source, external_id)
            if source_record is not None:
                self._store.bind_source_record(source_record, canonical_id)
        return Resolution(
            canonical_id,
            1.0,
            "new",
            True,
            tuple(sorted(candidates)),
            IdentityDecision.NEW,
            evidence=None,
            source_record_linked=False,
            candidate_method=candidate_method,
        )

    def _canonical_owners(self, owners: Iterable[str]) -> set[str]:
        """Collapse merged ids before comparing identifier owners."""
        return {self.merged_into(owner) or owner for owner in owners}

    @_atomic_identity_mutation
    def add_cannot_link(self, left_id: str, right_id: str, evidence: IdentityEvidence) -> bool:
        """Persist a symmetric cannot-link constraint; return whether it is new."""
        self.validate_cannot_link(left_id, right_id)
        pair = tuple(sorted((left_id, right_id)))
        is_new = pair not in self._store.cannot_links()
        self._store.add_cannot_link(left_id, right_id, evidence)
        return is_new

    def validate_cannot_link(self, left_id: str, right_id: str) -> None:
        """Validate a cannot-link pair without changing identity state."""
        if left_id == right_id:
            raise ValueError("cannot mark an entity as cannot-link with itself")
        for entity_id in (left_id, right_id):
            if self._store.get(entity_id) is None:
                raise ValueError(f"unknown canonical id {entity_id}")
        left = self.merged_into(left_id) or left_id
        right = self.merged_into(right_id) or right_id
        if left == right:
            raise ValueError("cannot-link pair already resolves to the same canonical entity")

    @_atomic_identity_mutation
    def replay_source_record_match(
        self,
        *,
        source_record: SourceRecordIdentity,
        entity_id: str,
        aliases: Iterable[str] = (),
        external_ids: Iterable[tuple[str, str]] = (),
    ) -> None:
        """Replay a logged source-record match and its justified bindings."""
        if self._store.get(entity_id) is None:
            raise ValueError(f"unknown canonical id {entity_id}")
        self._store.bind_source_record(source_record, entity_id)
        for alias in aliases:
            self._store.bind_alias(entity_id, alias)
        for source, external_id in external_ids:
            self._store.bind_external(entity_id, source, external_id)

    def state_snapshot(self) -> dict[str, object]:
        """Return deterministic identity state for replay-equivalence checks."""
        entities = {
            entity_id: {
                "entity_type": record.entity_type,
                "aliases": sorted(record.aliases),
                "external_ids": {
                    source: sorted(values) for source, values in sorted(record.external_ids.items())
                },
            }
            for entity_id in sorted(self._store.entity_ids())
            if (record := self._store.get(entity_id)) is not None
        }
        return {
            "entities": entities,
            "source_records": [
                {**key.to_dict(), "entity_id": entity_id}
                for key, entity_id in sorted(self._store.source_records().items())
            ],
            "redirects": dict(sorted(self._store.merge_redirects().items())),
            "cannot_links": [
                {"left_id": pair[0], "right_id": pair[1], **evidence.to_dict()}
                for pair, evidence in sorted(self._store.cannot_links().items())
            ],
        }

    @_atomic_identity_mutation
    def add_external_id(self, entity_id: str, source: str, external_id: str) -> None:
        """Attach an external identifier to a known canonical entity."""
        if self._store.get(entity_id) is None:
            raise ValueError(f"unknown canonical id {entity_id}")
        self._store.bind_external(entity_id, source, external_id)

    @_atomic_identity_mutation
    def merge_entities(
        self,
        survivor_id: str,
        duplicate_id: str,
        *,
        same_entity_evidence: IdentityEvidence | None = None,
    ) -> MergeOutcome:
        """Collapse ``duplicate_id`` into ``survivor_id`` (under-merge repair).

        Moves every alias and external-id binding owned by the duplicate to the
        survivor, so future exact lookups resolve to the survivor, and records
        a permanent redirect. The duplicate keeps its (now empty) record so its
        type stays introspectable but owns nothing of its own. Callers must
        persist the merge as an ``EntityMerged`` event so replays reproduce it.

        Returns:
            The bindings that were moved (for the event payload).

        Raises:
            ValueError: On missing evidence, self-merge, unknown ids, an
                already-merged duplicate, type conflict, or cannot-link.
        """
        outcome = self.preview_merge(
            survivor_id, duplicate_id, same_entity_evidence=same_entity_evidence
        )
        moved_aliases = outcome.moved_aliases
        moved_external_ids = outcome.moved_external_ids
        self._store.detach_bindings(duplicate_id)
        for alias in moved_aliases:
            self._store.bind_alias(survivor_id, alias)
        for source, ext in moved_external_ids:
            self._store.bind_external(survivor_id, source, ext)
        self._store.record_merge(duplicate_id, survivor_id)
        return outcome

    def preview_merge(
        self,
        survivor_id: str,
        duplicate_id: str,
        *,
        same_entity_evidence: IdentityEvidence | None = None,
    ) -> MergeOutcome:
        """Validate a merge and return its bindings without mutating the store."""
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
        if not isinstance(same_entity_evidence, IdentityEvidence):
            raise ValueError("merge requires explicit same-entity evidence")
        if self._cannot_link_blocks(survivor_id, duplicate_id):
            raise ValueError("cannot-link constraint blocks this merge")
        return MergeOutcome(
            moved_aliases=tuple(sorted(duplicate.aliases)),
            moved_external_ids=tuple(
                sorted(
                    (source, ext)
                    for source, values in duplicate.external_ids.items()
                    for ext in values
                )
            ),
        )

    def _cannot_link_blocks(self, survivor_id: str, duplicate_id: str) -> bool:
        """Check constraints between the current merged components."""
        return self._store.cannot_link_between_components(survivor_id, duplicate_id) is not None

    def cannot_link_evidence_between_components(
        self, left_id: str, right_id: str
    ) -> IdentityEvidence | None:
        """Return a constraint that prevents merging the two current components."""
        return self._store.cannot_link_between_components(left_id, right_id)

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

    @_atomic_identity_mutation
    def split_entity(
        self,
        survivor_id: str,
        duplicate_id: str,
        moved_aliases: tuple[str, ...] | list[str],
        moved_external_ids: tuple[tuple[str, str], ...] | list[tuple[str, str]],
        *,
        prepare_only: bool = False,
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
                if not prepare_only:
                    self._store.unbind_alias(alias, survivor_id)
                    self._store.bind_alias(duplicate_id, alias)
                restored_aliases.append(alias)
            else:
                retained_aliases.append(alias)

        for source, ext in moved_external_ids:
            if survivor_id in self._store.external_owners(source, ext):
                if not prepare_only:
                    self._store.unbind_external(source, ext, survivor_id)
                    self._store.bind_external(duplicate_id, source, ext)
                restored_external.append((source, ext))
            else:
                retained_external.append((source, ext))

        if not prepare_only:
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
