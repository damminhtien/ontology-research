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

Canonical ids use ``urn:world:entity:<uuid>`` - never database auto-increment.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

CONFIDENCE_EXTERNAL_ID = 1.0
CONFIDENCE_ALIAS = 0.95
REVIEW_THRESHOLD = 0.50

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_name(text: str) -> str:
    """Normalize a name for matching: casefold, strip punctuation, squeeze spaces."""
    cleaned = _NON_WORD.sub(" ", text.casefold())
    return " ".join(cleaned.split())


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
    external_ids: dict[str, str] = field(default_factory=dict)


class IdentityService:
    """In-memory identity registry with deterministic resolution rules.

    Sufficient for Phase 2 correctness work; persistence and serving scale
    are deferred to the platform layer without changing the contract.
    """

    def __init__(self) -> None:
        """Start with an empty in-memory registry."""
        self._records: dict[str, _Record] = {}
        self._by_alias: dict[str, str] = {}
        self._by_external: dict[str, str] = {}
        # Blocking index: token -> normalized aliases containing it. Candidate
        # generation for fuzzy matching only visits aliases sharing at least
        # one token with the query; a zero-token-overlap pair always scores 0
        # under the overlap coefficient, so blocking is exact (no false
        # negatives) and turns the O(aliases) scan into O(shared candidates).
        self._token_to_norms: dict[str, set[str]] = {}

    def register(
        self,
        *,
        entity_id: str,
        entity_type: str,
        aliases: list[str] | None = None,
        external_ids: Mapping[str, str] | None = None,
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
        for source, ext in (external_ids or {}).items():
            self._bind_external(entity_id, source, ext)

    def resolve(
        self,
        *,
        name: str | None = None,
        external_source: str | None = None,
        external_id: str | None = None,
        entity_type: str,
    ) -> Resolution:
        """Resolve one reference to a canonical identity, creating one if needed.

        Lookup order: external id, exact alias, fuzzy token overlap, then a new
        canonical entity. A supplied ``external_id`` asserts an externally
        unique identity (ADR-0006): when it misses, fuzzy name similarity is
        not merge evidence, so a new canonical entity is created instead of
        routing to review. Fuzzy review applies only when no external id is
        given; ambiguous fuzzy matches are never auto-merged.

        Raises:
            ValueError: If neither ``name`` nor ``external_id`` is provided.
        """
        if not name and not external_id:
            raise ValueError("resolve() requires a name or an external_id")

        if external_id is not None:
            key = f"{external_source or '*'}::{external_id}"
            hit = self._by_external.get(key)
            if hit is not None:
                return Resolution(hit, CONFIDENCE_EXTERNAL_ID, "external_id", False)

        if name:
            norm = normalize_name(name)
            alias_hit = self._by_alias.get(norm)
            if alias_hit is not None:
                return Resolution(alias_hit, CONFIDENCE_ALIAS, "alias", False)

            candidates = self._fuzzy_candidates(norm)
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

        canonical_id = f"urn:world:entity:{uuid.uuid4().hex}"
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

    def identity(self, entity_id: str) -> tuple[str, frozenset[str], dict[str, str]]:
        """Return (entity_type, aliases, external_ids) for a canonical id."""
        record = self._records[entity_id]
        return record.entity_type, frozenset(record.aliases), dict(record.external_ids)

    # -- internals ---------------------------------------------------------

    def _bind_alias(self, entity_id: str, alias: str) -> None:
        norm = normalize_name(alias)
        owner = self._by_alias.get(norm)
        if owner is not None and owner != entity_id:
            return  # first registration wins; conflicts surface via review flow
        if norm not in self._by_alias:
            for token in norm.split():
                self._token_to_norms.setdefault(token, set()).add(norm)
        self._by_alias.setdefault(norm, entity_id)
        self._records[entity_id].aliases.add(alias)

    def _bind_external(self, entity_id: str, source: str, external_id: str) -> None:
        key = f"{source}::{external_id}"
        owner = self._by_external.get(key)
        if owner is not None and owner != entity_id:
            return
        self._by_external.setdefault(key, entity_id)
        self._records[entity_id].external_ids[source] = external_id

    def _fuzzy_candidates(self, norm: str) -> list[tuple[str, float]]:
        """Return candidate (canonical_id, score) pairs above the review threshold.

        Scoring uses the overlap coefficient |A intersect B| / min(|A|, |B|),
        which suits subset-style name variants. Candidate generation is blocked
        through the token index: only aliases sharing at least one token with
        the query can score above zero, so the index prunes without changing
        results. Results are proposals only: callers must route them to review
        instead of auto-merging.
        """
        tokens = set(norm.split())
        if not tokens:
            return []
        shared: dict[str, int] = {}
        for token in tokens:
            for alias_norm in self._token_to_norms.get(token, ()):
                shared[alias_norm] = shared.get(alias_norm, 0) + 1
        found: list[tuple[str, float]] = []
        for alias_norm, overlap in shared.items():
            score = overlap / min(len(tokens), len(alias_norm.split()))
            if score >= REVIEW_THRESHOLD:
                found.append((self._by_alias[alias_norm], score))
        return found
