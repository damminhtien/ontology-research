"""Wikidata, GLEIF and Wikimedia connectors with source-specific mappers."""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from foundry.events import EVENT_TYPE_EXTERNAL_ID_BOUND, EventLog, SemanticEvent, make_event
from foundry.extraction import CandidateFact, Extractor
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline, IngestResult
from foundry.namespaces import ORGANIZATION_SUBSIDIARY_OF
from foundry.provenance import content_sha256, stable_source_record_key
from foundry.source_adapters import (
    ConnectorPage,
    RawRecord,
    SourceAdapterError,
    SourceContract,
    source_record_hash,
)
from foundry.wikidata import QID_TO_ENTITY_TYPE

QID_PATTERN = re.compile(r"Q[1-9]\d*\Z")
LEI_PATTERN = re.compile(r"[A-Z0-9]{18}\d{2}\Z")
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
GLEIF_API = "https://api.gleif.org/api/v1/lei-records"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "ontology-research-bot/0.1 (https://github.com/damminhtien/ontology-research)"


@dataclass(frozen=True)
class ExternalIdentifier:
    """An identifier mapped from a source record for explicit crosswalking."""

    namespace: str
    value: str
    rule: str
    evidence_uri: str


@dataclass(frozen=True)
class CrosswalkConflict:
    """Identifier owner conflict that must be routed to human review."""

    reason: str
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalEntityRecord:
    """Entity command produced by a domain mapper, before identity resolution."""

    source_id: str
    record_id: str
    source_uri: str
    content_hash: str
    name: str
    entity_type: str
    aliases: tuple[str, ...]
    external_source: str
    external_id: str
    crosswalks: tuple[ExternalIdentifier, ...] = ()


@dataclass(frozen=True)
class CanonicalDocumentRecord:
    """Document command with source revision and extraction proposals."""

    source_id: str
    record_id: str
    source_uri: str
    content_hash: str
    title: str
    text: str
    revision_id: str
    revision_timestamp: str
    candidate_facts: tuple[CandidateFact, ...]


class WikidataConnector:
    """Fetch small batches of exact Wikidata entities by QID."""

    def __init__(
        self,
        qids: Sequence[str],
        *,
        contract: SourceContract,
        http,
        api_url: str = WIKIDATA_API,
        batch_size: int = 50,
    ) -> None:
        """Configure a bounded wbgetentities connector."""
        if not qids or any(not QID_PATTERN.fullmatch(qid) for qid in qids):
            raise ValueError("Wikidata connector requires one or more valid QIDs")
        if batch_size < 1 or batch_size > 50:
            raise ValueError("Wikidata batch_size must be in [1, 50]")
        self.qids = tuple(dict.fromkeys(qids))
        self.contract = contract
        self._http = http
        self._api_url = api_url
        self._batch_size = batch_size
        self.request_fingerprint = _request_hash(
            {"source": contract.id, "api_url": api_url, "qids": self.qids, "batch_size": batch_size}
        )

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        """Fetch one page of raw Wikidata JSON, preserving the response bytes."""
        try:
            offset = int(cursor or 0)
        except ValueError as exc:
            raise SourceAdapterError(f"invalid Wikidata cursor {cursor!r}") from exc
        if offset < 0 or offset >= len(self.qids):
            raise SourceAdapterError(f"Wikidata cursor out of range: {offset}")
        batch = self.qids[offset : offset + self._batch_size]
        url = (
            self._api_url
            + "?"
            + urllib.parse.urlencode(
                {
                    "action": "wbgetentities",
                    "ids": "|".join(batch),
                    "props": "labels|aliases|claims",
                    "languages": "en|vi",
                    "format": "json",
                }
            )
        )
        body, _payload = self._http.get_json(url)
        next_offset = offset + len(batch)
        fetched_at = datetime.now(UTC).isoformat()
        record = RawRecord(
            source_id=self.contract.id,
            record_id=f"entities:{offset}-{next_offset}",
            source_uri=self._api_url,
            schema_version=self.contract.record_schema,
            body=body,
            retrieved_at=fetched_at,
        )
        complete = next_offset >= len(self.qids)
        return ConnectorPage((record,), None if complete else str(next_offset), complete)


class GleifConnector:
    """Fetch one requested LEI record through the public GLEIF API."""

    def __init__(
        self, lei: str, *, contract: SourceContract, http, api_url: str = GLEIF_API
    ) -> None:
        """Configure one LEI lookup."""
        if not valid_lei(lei):
            raise ValueError(f"invalid LEI {lei!r}")
        self.lei = lei
        self.contract = contract
        self._http = http
        self._api_url = api_url.rstrip("/")
        self.request_fingerprint = _request_hash(
            {"source": contract.id, "api_url": self._api_url, "lei": lei}
        )

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        """Fetch the exact LEI endpoint and return its untouched JSON body."""
        if cursor is not None:
            raise SourceAdapterError("GLEIF single-record connector does not accept a cursor")
        url = f"{self._api_url}/{self.lei}"
        body, _payload = self._http.get_json(url, accept="application/vnd.api+json")
        record = RawRecord(
            source_id=self.contract.id,
            record_id=self.lei,
            source_uri=url,
            schema_version=self.contract.record_schema,
            body=body,
            retrieved_at=datetime.now(UTC).isoformat(),
        )
        return ConnectorPage((record,), None, True)


class WikipediaConnector:
    """Fetch the latest plain-text extract and revision metadata for one page."""

    def __init__(
        self,
        title: str,
        *,
        contract: SourceContract,
        http,
        api_url: str = WIKIPEDIA_API,
    ) -> None:
        """Configure one English Wikipedia article lookup."""
        if not title.strip():
            raise ValueError("Wikipedia title must be non-empty")
        self.title = title.strip()
        self.contract = contract
        self._http = http
        self._api_url = api_url
        self.request_fingerprint = _request_hash(
            {"source": contract.id, "api_url": api_url, "title": self.title}
        )

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        """Fetch a latest-article extract with page, revision and Wikidata IDs."""
        if cursor is not None:
            raise SourceAdapterError("Wikipedia single-page connector does not accept a cursor")
        params = {
            "action": "query",
            "prop": "extracts|revisions|pageprops",
            "explaintext": "1",
            "rvprop": "timestamp|ids",
            "rvslots": "main",
            "ppprop": "wikibase_item",
            "format": "json",
            "formatversion": "2",
            "titles": self.title,
        }
        url = self._api_url + "?" + urllib.parse.urlencode(params)
        body, _payload = self._http.get_json(url)
        article_url = urllib.parse.quote(self.title.replace(" ", "_"))
        record = RawRecord(
            source_id=self.contract.id,
            record_id=f"article:{self.title.casefold()}",
            source_uri=f"https://en.wikipedia.org/wiki/{article_url}",
            schema_version=self.contract.record_schema,
            body=body,
            retrieved_at=datetime.now(UTC).isoformat(),
        )
        return ConnectorPage((record,), None, True)


class WikidataEntityMapper:
    """Map raw Wikidata entity objects to canonical organization records."""

    mapping_version = "1"

    def map(self, record: RawRecord) -> Sequence[CanonicalEntityRecord]:
        """Apply the source type allowlist and extract identifiers and aliases."""
        payload = _json_object(record)
        entities = payload.get("entities")
        if not isinstance(entities, dict):
            raise SourceAdapterError("Wikidata response is missing its entities object")
        results: list[CanonicalEntityRecord] = []
        for qid, raw_entity in sorted(entities.items()):
            if not QID_PATTERN.fullmatch(qid) or not isinstance(raw_entity, dict):
                continue
            labels = raw_entity.get("labels") or {}
            if not isinstance(labels, dict):
                continue
            name = next(
                (
                    labels[language]["value"].strip()
                    for language in ("vi", "en")
                    if isinstance(labels.get(language), dict)
                    and isinstance(labels[language].get("value"), str)
                    and labels[language]["value"].strip()
                ),
                "",
            )
            claims = raw_entity.get("claims") or {}
            if not name or not isinstance(claims, dict):
                continue
            type_qids = _claim_item_ids(claims, "P31")
            entity_type = next(
                (QID_TO_ENTITY_TYPE[q] for q in type_qids if q in QID_TO_ENTITY_TYPE), None
            )
            if entity_type != "Organization":
                continue
            aliases: list[str] = []
            raw_aliases = raw_entity.get("aliases") or {}
            if isinstance(raw_aliases, dict):
                for language in ("vi", "en"):
                    values = raw_aliases.get(language) or []
                    if isinstance(values, list):
                        aliases.extend(
                            value["value"].strip()
                            for value in values
                            if isinstance(value, dict)
                            and isinstance(value.get("value"), str)
                            and value["value"].strip()
                        )
            aliases = list(dict.fromkeys(alias for alias in aliases if alias != name))
            lei_values = _claim_strings(claims, "P1278")
            crosswalks = tuple(
                ExternalIdentifier(
                    namespace="gleif-lei",
                    value=lei,
                    rule="wikidata:P1278-to-gleif-lei",
                    evidence_uri=f"http://www.wikidata.org/entity/{qid}",
                )
                for lei in dict.fromkeys(lei_values)
                if valid_lei(lei)
            )
            results.append(
                CanonicalEntityRecord(
                    source_id=record.source_id,
                    record_id=qid,
                    source_uri=f"http://www.wikidata.org/entity/{qid}",
                    content_hash=source_record_hash(raw_entity),
                    name=name,
                    entity_type=entity_type,
                    aliases=tuple(aliases),
                    external_source="wikidata",
                    external_id=qid,
                    crosswalks=crosswalks,
                )
            )
        return tuple(results)


class GleifEntityMapper:
    """Map GLEIF Level 1 records to canonical organizations."""

    mapping_version = "2"

    def map(self, record: RawRecord) -> Sequence[CanonicalEntityRecord]:
        """Validate the requested LEI and legal name before producing a command."""
        payload = _json_object(record)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("attributes"), dict):
            raise SourceAdapterError("GLEIF response is missing data.attributes")
        lei = str(data.get("id") or "")
        attributes = data["attributes"]
        entity = attributes.get("entity")
        if not isinstance(entity, dict) or not valid_lei(lei):
            raise SourceAdapterError("GLEIF record has an invalid entity or LEI")
        if lei != record.record_id:
            raise SourceAdapterError("GLEIF response LEI does not match the requested record")
        legal_name = entity.get("legalName")
        name = legal_name.get("name", "").strip() if isinstance(legal_name, dict) else ""
        if not name:
            raise SourceAdapterError(f"GLEIF LEI {lei} has no legal name")
        status = entity.get("status")
        if not isinstance(status, str) or not status.strip():
            raise SourceAdapterError(f"GLEIF LEI {lei} has no entity status")
        aliases = tuple(
            dict.fromkeys(
                item["name"].strip()
                for item in entity.get("otherNames", [])
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"].strip()
                and item["name"].strip() != name
            )
        )
        return (
            CanonicalEntityRecord(
                source_id=record.source_id,
                record_id=lei,
                source_uri=record.source_uri,
                content_hash=record.content_hash,
                name=name,
                entity_type="Organization",
                aliases=aliases,
                external_source="gleif-lei",
                external_id=lei,
            ),
        )


class WikipediaDocumentMapper:
    """Map one Wikipedia revision to a document and bounded fact proposals."""

    mapping_version = "3"

    SUBSIDIARY_OF = re.compile(
        r"(?P<subject>[^.!?\n]{1,100}?)\s+is\s+(?:a|an)\s+"
        r"(?:[\w-]+\s+){0,4}subsidiary of\s+"
        r"(?P<object>[^.;,\n]{2,80}?)"
        r"(?=\s+(?:that\s+was|formed\s+in|since\s+\d{4})\b|[.,;\n]|$)",
        re.IGNORECASE,
    )

    def map(self, record: RawRecord) -> Sequence[CanonicalDocumentRecord]:
        """Preserve revision provenance and extract only explicit subsidiary claims."""
        payload = _json_object(record)
        query = payload.get("query")
        pages = query.get("pages") if isinstance(query, dict) else None
        page = pages[0] if isinstance(pages, list) and pages else None
        if not isinstance(page, dict) or page.get("missing") is True:
            raise SourceAdapterError("Wikipedia response is missing the requested page")
        title = page.get("title")
        page_id = page.get("pageid")
        revisions = page.get("revisions")
        revision = revisions[0] if isinstance(revisions, list) and revisions else None
        if (
            not isinstance(title, str)
            or not isinstance(page_id, int)
            or not isinstance(revision, dict)
        ):
            raise SourceAdapterError("Wikipedia response is missing page or revision metadata")
        revision_id = revision.get("revid")
        revision_timestamp = revision.get("timestamp")
        text = page.get("extract")
        pageprops = page.get("pageprops")
        page_qid = pageprops.get("wikibase_item") if isinstance(pageprops, dict) else None
        if (
            not isinstance(revision_id, int)
            or not isinstance(revision_timestamp, str)
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise SourceAdapterError("Wikipedia response is missing revision text or identifiers")
        if page_qid is not None and (
            not isinstance(page_qid, str) or not QID_PATTERN.fullmatch(page_qid)
        ):
            raise SourceAdapterError("Wikipedia page has an invalid Wikidata item identifier")
        _parse_timestamp(revision_timestamp)
        candidates = tuple(
            self._extract_subsidiary_claims(title, text, revision_timestamp, page_qid)
        )
        url = record.source_uri
        return (
            CanonicalDocumentRecord(
                source_id=record.source_id,
                record_id=f"enwiki:{page_id}",
                source_uri=url,
                content_hash=content_sha256(text),
                title=title,
                text=text,
                revision_id=str(revision_id),
                revision_timestamp=revision_timestamp,
                candidate_facts=candidates,
            ),
        )

    def _extract_subsidiary_claims(
        self, title: str, text: str, revision_timestamp: str, page_qid: str | None
    ) -> list[CandidateFact]:
        """Map first-paragraph subsidiary statements with exact text spans."""
        if page_qid is None:
            return []
        paragraph = text.split("\n\n", 1)[0]
        candidates: list[CandidateFact] = []
        for match in self.SUBSIDIARY_OF.finditer(paragraph):
            subject = match.group("subject").strip()
            if subject.casefold() in {"it", "this company", "the company"}:
                subject = title
            object_name = re.sub(r"^(?:the|a|an)\s+", "", match.group("object").strip(), flags=re.I)
            if not subject or not object_name:
                continue
            start, end = match.span()
            sentence_end = paragraph.find(".", end)
            if sentence_end != -1:
                end = sentence_end
            raw_span = paragraph[start:end]
            leading_space = len(raw_span) - len(raw_span.lstrip())
            exact_text = raw_span.strip()
            start += leading_space
            end = start + len(exact_text)
            candidates.append(
                CandidateFact(
                    subject_name=subject,
                    subject_external_source="wikidata",
                    subject_external_id=page_qid,
                    predicate_iri=ORGANIZATION_SUBSIDIARY_OF,
                    object_kind="entity",
                    object_value=object_name,
                    valid_from=revision_timestamp,
                    model_score=0.0,
                    snippet=exact_text,
                    evidence_span={
                        "start": start,
                        "end": end,
                        "unit": "character",
                        "exact_text": exact_text,
                    },
                )
            )
        return candidates


class CandidateFactExtractor(Extractor):
    """Expose source-mapper proposals through the canonical document pipeline."""

    def __init__(self, candidates: Sequence[CandidateFact]) -> None:
        """Keep immutable mapped proposals for one source document."""
        self._candidates = tuple(candidates)

    def extract(self, text: str) -> list[CandidateFact]:
        """Return proposals only when the mapper's source text still matches."""
        if any(
            fact.evidence_span is not None
            and text[fact.evidence_span["start"] : fact.evidence_span["end"]]
            != fact.evidence_span["exact_text"]
            for fact in self._candidates
        ):
            raise SourceAdapterError("mapped evidence span no longer matches document text")
        return list(self._candidates)


def ingest_entity_record(
    pipeline: IngestionPipeline,
    identity: IdentityService,
    log: EventLog,
    record: CanonicalEntityRecord,
) -> IngestResult:
    """Commit one mapped entity and durable, conflict-checked crosswalk IDs."""
    result = pipeline.ingest_entity(
        name=record.name,
        entity_type=record.entity_type,
        source_id=record.source_id,
        record_id=record.record_id,
        external_source=record.external_source,
        external_id=record.external_id,
        aliases=list(record.aliases),
        content_hash=record.content_hash,
    )
    if result.accepted:
        for crosswalk in record.crosswalks:
            binding = bind_crosswalk_identifier(
                identity,
                log,
                entity_id=result.canonical_id,
                identifier=crosswalk,
                source_id=record.source_id,
                record_id=record.record_id,
                content_hash=record.content_hash,
            )
            if isinstance(binding, CrosswalkConflict):
                pipeline.queue_identity_review(
                    reference_name=record.name,
                    external_source=crosswalk.namespace,
                    external_id=crosswalk.value,
                    entity_type=record.entity_type,
                    candidates=binding.candidates,
                    reason=binding.reason,
                    idempotency_key=(
                        "crosswalk-review:"
                        + content_sha256(
                            json.dumps(
                                {
                                    "source_record_key": stable_source_record_key(
                                        record.source_id, record.record_id
                                    ),
                                    "content_hash": record.content_hash,
                                    "namespace": crosswalk.namespace,
                                    "external_id": crosswalk.value,
                                    "candidates": binding.candidates,
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        )
                    ),
                )
    return result


def bind_crosswalk_identifier(
    identity: IdentityService,
    log: EventLog,
    *,
    entity_id: str,
    identifier: ExternalIdentifier,
    source_id: str,
    record_id: str,
    content_hash: str,
) -> SemanticEvent | CrosswalkConflict | None:
    """Durably bind an exact cross-source identifier after collision checks."""
    if identifier.namespace == "gleif-lei" and not valid_lei(identifier.value):
        raise SourceAdapterError(f"invalid LEI crosswalk {identifier.value!r}")
    owners = identity.store.external_owners(identifier.namespace, identifier.value)
    canonical_owners = {identity.merged_into(owner) or owner for owner in owners}
    canonical_entity = identity.merged_into(entity_id) or entity_id
    if canonical_owners:
        if canonical_owners == {canonical_entity}:
            return None
        return CrosswalkConflict(
            reason=(
                f"crosswalk {identifier.namespace}:{identifier.value} is already bound to "
                "a different entity"
            ),
            candidates=tuple(sorted(canonical_owners)),
        )
    payload = {
        "entity_id": canonical_entity,
        "source": identifier.namespace,
        "external_id": identifier.value,
        "source_record_key": stable_source_record_key(source_id, record_id),
        "content_hash": content_hash,
        "evidence_uri": identifier.evidence_uri,
        "mapping_rule": identifier.rule,
    }
    fingerprint = content_sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    committed = log.commit(
        [make_event(EVENT_TYPE_EXTERNAL_ID_BOUND, payload)],
        idempotency_key=(
            f"crosswalk:{payload['source_record_key']}:{identifier.namespace}:"
            f"{identifier.value}:{content_hash}"
        ),
        request_fingerprint=fingerprint,
    ).events[0]
    identity.add_external_id(canonical_entity, identifier.namespace, identifier.value)
    return committed


def valid_lei(value: str) -> bool:
    """Validate a 20-character LEI and its ISO 17442 mod-97 check digits."""
    if not LEI_PATTERN.fullmatch(value):
        return False
    expanded = "".join(str(int(char)) if char.isdigit() else str(ord(char) - 55) for char in value)
    return int(expanded) % 97 == 1


def _json_object(record: RawRecord) -> dict[str, object]:
    """Decode one archived raw JSON response with source context."""
    try:
        payload = json.loads(record.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceAdapterError(
            f"invalid JSON in {record.source_id}/{record.record_id}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise SourceAdapterError(f"expected JSON object in {record.source_id}/{record.record_id}")
    return payload


def _request_hash(value: dict[str, object]) -> str:
    """Hash connector parameters so a checkpoint cannot resume another query."""
    return content_sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _claim_item_ids(claims: dict[str, object], property_id: str) -> tuple[str, ...]:
    """Read item-valued claims from Wikidata's entity JSON representation."""
    values = claims.get(property_id) or []
    if not isinstance(values, list):
        return ()
    return tuple(
        value["id"]
        for item in values
        if isinstance(item, dict)
        and isinstance(item.get("mainsnak"), dict)
        and isinstance((datavalue := item["mainsnak"].get("datavalue")), dict)
        and isinstance((value := datavalue.get("value")), dict)
        and isinstance(value.get("id"), str)
    )


def _claim_strings(claims: dict[str, object], property_id: str) -> tuple[str, ...]:
    """Read string-valued claims from Wikidata's entity JSON representation."""
    values = claims.get(property_id) or []
    if not isinstance(values, list):
        return ()
    return tuple(
        value.strip()
        for item in values
        if isinstance(item, dict)
        and isinstance(item.get("mainsnak"), dict)
        and isinstance((datavalue := item["mainsnak"].get("datavalue")), dict)
        and isinstance((value := datavalue.get("value")), str)
        and value.strip()
    )


def _parse_timestamp(value: str) -> datetime:
    """Parse a MediaWiki ISO timestamp as an aware UTC instant."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceAdapterError(f"invalid source timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise SourceAdapterError(f"source timestamp must have timezone: {value!r}")
    return parsed.astimezone(UTC)
