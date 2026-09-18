"""Candidate-fact extraction from unstructured documents (Phase 2).

Contract principle from the roadmap: **LLM chỉ đề xuất; semantic system quyết
định acceptance.** An extractor *proposes* candidate facts; the canonical
pipeline decides. Therefore this module enforces:

- no extractor output ever mints an entity or merges automatically —
  unresolved references route to the durable review queue;
- extracted facts carry capped confidence and full provenance (the citing
  document id), so they are visibly weaker than structured-source facts;
- every accepted candidate passes the same SHACL assertion gate as
  structured data.

:class:`PatternExtractor` is the deterministic default (Vietnamese/English
report patterns) so the document lane works without any LLM. An LLM backend
plugs in through :class:`LlmExtractor` by injecting a completion callable —
the pipeline never changes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from foundry.namespaces import CORE_LOCATED_AT, CORE_MEMBER_OF

CAP_EXTRACTED_CONFIDENCE = 0.7


@dataclass(frozen=True)
class CandidateFact:
    """One proposed fact from an unstructured document.

    ``predicate_iri`` is an absolute relation IRI (never a local name): an
    extractor proposes a *relation the ontology declares*, and the pipeline
    rejects anything outside the registered model.
    """

    subject_name: str
    predicate_iri: str
    object_kind: str  # entity | location | literal
    object_value: str
    valid_from: str | None
    confidence: float
    snippet: str = ""


class Extractor(Protocol):
    """Anything that can propose candidate facts from document text."""

    def extract(self, text: str) -> list[CandidateFact]:
        """Return proposed facts (the pipeline gates every one of them)."""
        ...


@dataclass(frozen=True)
class _Pattern:
    regex: re.Pattern[str]
    predicate_iri: str
    object_kind: str


class PatternExtractor:
    """Deterministic regex extractor for Vietnamese/English report patterns.

    Zero-dependency default so documents ingest without an LLM. Every match
    yields exactly one candidate; the pipeline still validates identity and
    SHACL, so a regex false positive can never corrupt the ledger — it just
    produces a low-confidence, visibly-sourced assertion.
    """

    _PATTERNS: tuple[_Pattern, ...] = (
        _Pattern(
            re.compile(r"(?P<subj>[^.,;\n]{3,80}?)\s+đặt tại\s+(?P<obj>[^.,;\n]{2,80})"),
            CORE_LOCATED_AT,
            "location",
        ),
        _Pattern(
            re.compile(r"(?P<subj>[^.,;\n]{3,80}?)\s+có trụ sở tại\s+(?P<obj>[^.,;\n]{2,80})"),
            CORE_LOCATED_AT,
            "location",
        ),
        _Pattern(
            re.compile(
                r"(?P<subj>[^.,;\n]{3,80}?)\s+hiện (?:đóng|nằm) tại\s+(?P<obj>[^.,;\n]{2,80})"
            ),
            CORE_LOCATED_AT,
            "location",
        ),
        _Pattern(
            re.compile(
                r"(?P<subj>[^.,;\n]{3,80}?)\s+is\s+(?:based|stationed|located)\s+at\s+"
                r"(?P<obj>[^.,;\n]{2,80})",
                re.IGNORECASE,
            ),
            CORE_LOCATED_AT,
            "location",
        ),
        _Pattern(
            re.compile(r"(?P<subj>[^.,;\n]{3,80}?)\s+thuộc\s+(?P<obj>[^.,;\n]{2,80})"),
            CORE_MEMBER_OF,
            "entity",
        ),
        _Pattern(
            re.compile(
                r"(?P<subj>[^.,;\n]{3,80}?)\s+(?:is\s+)?part\s+of\s+(?P<obj>[^.,;\n]{2,80})",
                re.IGNORECASE,
            ),
            CORE_MEMBER_OF,
            "entity",
        ),
    )
    _DATE = re.compile(
        r"từ\s+(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?)"
        r"|since\s+(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?)",
        re.IGNORECASE,
    )
    _TRAILING_DATE = re.compile(
        r"\s+(?:từ|since)\s+(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?)\s*[.。]?\s*$",
        re.IGNORECASE,
    )

    def extract(self, text: str) -> list[CandidateFact]:
        """Run every pattern; a date in the matched sentence becomes valid_from."""
        candidates: list[CandidateFact] = []
        for pattern in self._PATTERNS:
            for match in pattern.regex.finditer(text):
                subject = match.group("subj").strip()
                obj = match.group("obj").strip()
                if not subject or not obj:
                    continue
                sentence = match.group(0)
                date_match = self._DATE.search(sentence)
                valid_from = (date_match.group(1) or date_match.group(2)) if date_match else None
                if valid_from and "T" not in valid_from:
                    valid_from += "T00:00:00Z"
                # the object group may have swallowed the trailing date clause
                trailing = self._TRAILING_DATE.search(obj)
                if trailing is not None:
                    valid_from = valid_from or (
                        trailing.group(1) + ("T00:00:00Z" if "T" not in trailing.group(1) else "")
                    )
                    obj = obj[: trailing.start()].rstrip()
                candidates.append(
                    CandidateFact(
                        subject_name=subject,
                        predicate_iri=pattern.predicate_iri,
                        object_kind=pattern.object_kind,
                        object_value=obj,
                        valid_from=valid_from,
                        confidence=CAP_EXTRACTED_CONFIDENCE,
                        snippet=sentence,
                    )
                )
        return candidates


class LlmExtractor:
    """LLM-backed extractor behind the same protocol.

    The completion callable receives the document text and must return a JSON
    array of candidate facts — nothing else. Each item carries
    ``predicate_iri``: an absolute relation IRI the ontology declares, since a
    proposal naming a relation the registered model does not define is rejected
    (never mapped onto a guessed IRI). The call itself is injected, so tests and
    offline environments never touch a network, and switching LLM vendors never
    touches the pipeline.

    Raises:
        ValueError: At construction when no completion callable is configured.
    """

    def __init__(self, complete: Callable[[str], str] | None = None) -> None:
        """Bind the LLM completion callable (``text -> JSON candidate list``)."""
        if complete is None:
            raise ValueError(
                "LlmExtractor requires a completion callable; "
                "configure an LLM backend or use PatternExtractor"
            )
        self._complete = complete

    def extract(self, text: str) -> list[CandidateFact]:
        """Call the injected completion and parse its JSON candidate list.

        Raises:
            ValueError: On non-JSON completions or a non-array body.
        """
        raw = self._complete(text)
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM completion is not a JSON candidate list: {exc}") from exc
        if not isinstance(items, list):
            raise ValueError("LLM completion must be a JSON array of candidate facts")
        candidates: list[CandidateFact] = []
        for item in items:
            candidate = CandidateFact(
                subject_name=str(item.get("subject_name", "")).strip(),
                predicate_iri=str(item.get("predicate_iri", "")).strip(),
                object_kind=str(item.get("object_kind", "")).strip(),
                object_value=str(item.get("object_value", "")).strip(),
                valid_from=item.get("valid_from"),
                confidence=min(float(item.get("confidence", 0.0)), CAP_EXTRACTED_CONFIDENCE),
                snippet=str(item.get("snippet", ""))[:200],
            )
            if candidate.subject_name and candidate.predicate_iri and candidate.object_value:
                candidates.append(candidate)
        return candidates
