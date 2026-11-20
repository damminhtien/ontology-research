"""Replay regional crawl evidence into the shared canonical event log."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foundry.events import (
    EVENT_TYPE_ASSERTION_MADE,
    EVENT_TYPE_DOCUMENT_REGISTERED,
    EVENT_TYPE_ENTITY_CREATED,
    EVENT_TYPE_SOURCE_RECORD_MATCHED,
    EventLog,
    SemanticEvent,
)
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline, IngestResult
from foundry.merge import rebuild_identity
from foundry.namespaces import CORE_LOCATED_AT
from foundry.osint_sources import (
    ICAO24_NAMESPACE,
    LEGACY_ICAO24_NAMESPACES,
    AdsbPositionMapper,
    OpenSkyMapper,
    PositionRecord,
    aircraft_identity_content_hash,
)
from foundry.provenance import content_sha256
from foundry.source_adapters import (
    RawRecord,
    SourceAdapterError,
    SourceContract,
    load_source_contracts,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
AIRCRAFT_SOURCE_IDS = frozenset({"opensky", "adsb-lol", "adsb-fi"})
LEGACY_NAMESPACE_BY_AIRCRAFT_SOURCE = {
    "opensky": "opensky-icao24",
    "adsb-lol": "adsb-lol-icao24",
    "adsb-fi": "adsb-fi-icao24",
}
SUPPORTED_EVENT_TYPES = frozenset(
    {
        EVENT_TYPE_ENTITY_CREATED,
        EVENT_TYPE_SOURCE_RECORD_MATCHED,
        EVENT_TYPE_ASSERTION_MADE,
        EVENT_TYPE_DOCUMENT_REGISTERED,
        "ResolutionReviewQueued",
    }
)


@dataclass(frozen=True)
class ConsolidationResult:
    """Counts for an idempotent consolidation pass."""

    source_logs: int
    source_events: int
    raw_snapshots: int
    mapped_positions: int
    entities_created: int
    entity_records_linked: int
    identities_reused: int
    assertions_written: int
    documents_written: int
    reviews_written: int
    stale_aircraft_reviews_skipped: int
    new_events: int
    deduplicated_records: int
    event_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        """Serialize report counts for CLI output and an external manifest."""
        return {
            "source_logs": self.source_logs,
            "source_events": self.source_events,
            "raw_snapshots": self.raw_snapshots,
            "mapped_positions": self.mapped_positions,
            "entities_created": self.entities_created,
            "entity_records_linked": self.entity_records_linked,
            "identities_reused": self.identities_reused,
            "assertions_written": self.assertions_written,
            "documents_written": self.documents_written,
            "reviews_written": self.reviews_written,
            "stale_aircraft_reviews_skipped": self.stale_aircraft_reviews_skipped,
            "new_events": self.new_events,
            "deduplicated_records": self.deduplicated_records,
            "event_counts": self.event_counts,
        }


def ingest_position(
    pipeline: IngestionPipeline, position: PositionRecord
) -> tuple[IngestResult, IngestResult]:
    """Resolve one ADS-B position and append it against its canonical entity."""
    entity = pipeline.ingest_entity(
        name=position.entity_name,
        entity_type="Platform",
        source_id=position.source_id,
        record_id=position.source_record_id,
        external_source=ICAO24_NAMESPACE,
        external_id=position.external_id,
        content_hash=position.identity_content_hash,
    )
    if not entity.accepted:
        raise SourceAdapterError(
            f"{position.source_id} entity {position.source_record_id} was not resolved: "
            f"{entity.reason}"
        )
    assertion = pipeline.ingest_assertion(
        subject_id=entity.canonical_id,
        predicate_iri=CORE_LOCATED_AT,
        object_kind="location",
        object_value=position.location_iri,
        valid_from=position.observed_at,
        valid_until=position.valid_until,
        source_ids=[position.source_id],
        evidence=[{"source_id": position.source_id, "locator": position.locator}],
        evidence_quality="unknown",
        idempotency_key=(f"osint-position:{position.source_id}:{position.position_record_id}"),
    )
    if not assertion.accepted:
        raise SourceAdapterError(
            f"{position.source_id} position {position.position_record_id} rejected: "
            f"{assertion.reason}"
        )
    return entity, assertion


def consolidate_crawl_data(
    *,
    crawl_root: Path,
    destination: EventLog,
    ontology_path: Path,
    shapes_path: Path,
) -> ConsolidationResult:
    """Reconcile historical run logs and raw aircraft snapshots idempotently.

    Original run logs and raw archives are read-only inputs. Entity and document
    commands are replayed through the normal write contracts, which gives
    repeated source records stable canonical IDs and deduplicates document
    versions by source key and content hash.
    """
    root = crawl_root.expanduser().resolve()
    run_roots = sorted(path for path in root.iterdir() if path.is_dir()) if root.exists() else []
    source_logs = [path / "events.jsonl" for path in run_roots if (path / "events.jsonl").is_file()]
    _preflight_logs(source_logs)
    contracts = load_source_contracts(REPO_ROOT / "config" / "source_registry.json")
    replayable_aircraft_ids: set[tuple[str, str]] = set()
    for run_root in run_roots:
        for source_id in sorted(AIRCRAFT_SOURCE_IDS):
            manifest_path = run_root / "raw" / source_id / "manifest.jsonl"
            if manifest_path.is_file():
                mapper = _aircraft_mapper(source_id)
                for raw in _read_archive(manifest_path, source_id, contracts[source_id]):
                    replayable_aircraft_ids.update(
                        (LEGACY_NAMESPACE_BY_AIRCRAFT_SOURCE[source_id], position.external_id)
                        for position in mapper.map(raw)
                    )

    identity = rebuild_identity(destination)
    pipeline = IngestionPipeline(
        identity=identity,
        log=destination,
        ontology_path=ontology_path,
        shapes_path=shapes_path,
    )
    start_sequence = destination.last_sequence
    counters: Counter[str] = Counter()
    for source_path in _sort_logs_by_first_event(source_logs):
        source_log = EventLog(source_path)
        source_identity = rebuild_identity(source_log)
        entity_map: dict[str, str] = {}
        for event in source_log.iter_all():
            counters["source_events"] += 1
            if event.event_type in {EVENT_TYPE_ENTITY_CREATED, EVENT_TYPE_SOURCE_RECORD_MATCHED}:
                _replay_entity(event, source_identity, entity_map, pipeline, identity, counters)
            elif event.event_type == EVENT_TYPE_DOCUMENT_REGISTERED:
                _replay_document(event, pipeline, counters)
            elif event.event_type == EVENT_TYPE_ASSERTION_MADE:
                _replay_assertion(event, entity_map, identity, pipeline, counters)
            elif event.event_type == "ResolutionReviewQueued":
                if _is_stale_aircraft_review(event, replayable_aircraft_ids):
                    counters["stale_aircraft_reviews_skipped"] += 1
                else:
                    _replay_review(event, entity_map, identity, pipeline, counters)

    mapped_positions = 0
    for run_root in run_roots:
        for source_id in sorted(AIRCRAFT_SOURCE_IDS):
            manifest_path = run_root / "raw" / source_id / "manifest.jsonl"
            if not manifest_path.is_file():
                continue
            for raw in _read_archive(manifest_path, source_id, contracts[source_id]):
                counters["raw_snapshots"] += 1
                mapper = _aircraft_mapper(source_id)
                for position in mapper.map(raw):
                    counters["mapped_positions"] += 1
                    entity, assertion = ingest_position(pipeline, position)
                    counters["entity_records_linked"] += 1
                    counters["entities_created"] += int(entity.is_new)
                    counters["identities_reused"] += int(not entity.is_new)
                    counters["assertions_written"] += int(not assertion.replayed)
                    counters["deduplicated_records"] += int(assertion.replayed)
                    mapped_positions += 1

    event_counts: Counter[str] = Counter()
    new_event_count = 0
    for event in destination.iter_after_sequence(start_sequence):
        new_event_count += 1
        event_counts[event.event_type] += 1
    return ConsolidationResult(
        source_logs=len(source_logs),
        source_events=counters["source_events"],
        raw_snapshots=counters["raw_snapshots"],
        mapped_positions=mapped_positions,
        entities_created=counters["entities_created"],
        entity_records_linked=counters["entity_records_linked"],
        identities_reused=counters["identities_reused"],
        assertions_written=counters["assertions_written"],
        documents_written=counters["documents_written"],
        reviews_written=counters["reviews_written"],
        stale_aircraft_reviews_skipped=counters["stale_aircraft_reviews_skipped"],
        new_events=new_event_count,
        deduplicated_records=counters["deduplicated_records"],
        event_counts=dict(sorted(event_counts.items())),
    )


def _preflight_logs(paths: list[Path]) -> None:
    """Reject unsupported event contracts before writing to the destination."""
    for path in paths:
        for event in EventLog(path).iter_all():
            if event.event_type not in SUPPORTED_EVENT_TYPES:
                raise SourceAdapterError(
                    f"cannot consolidate {path}: unsupported event type {event.event_type}"
                )


def _sort_logs_by_first_event(paths: list[Path]) -> list[Path]:
    """Use first recorded time to prefer the earliest known identity assignment."""

    def first_recorded(path: Path) -> str:
        first = next(EventLog(path).iter_all(), None)
        return first.occurred_at if first else ""

    return sorted(paths, key=first_recorded)


def _replay_entity(
    event: SemanticEvent,
    source_identity: IdentityService,
    entity_map: dict[str, str],
    pipeline: IngestionPipeline,
    identity: IdentityService,
    counters: Counter[str],
) -> None:
    payload = event.payload
    local_id = str(payload["entity_id"])
    if event.event_type == EVENT_TYPE_ENTITY_CREATED:
        entity_type = str(payload["entity_type"])
    else:
        entity_type = source_identity.identity(local_id)[0]
    external_ids = payload.get("external_ids") or []
    if len(external_ids) > 1:
        raise SourceAdapterError(f"multiple external identifiers in {event.event_id}")
    external_source = external_id = None
    if external_ids:
        external_source = _canonical_external_namespace(str(external_ids[0]["source"]))
        external_id = str(external_ids[0]["external_id"])
    name = str(payload["name"])
    content_hash = payload.get("content_hash")
    if external_source == ICAO24_NAMESPACE and external_id is not None:
        external_id = external_id.strip().lower()
        name = f"Aircraft {external_id}"
        content_hash = aircraft_identity_content_hash(external_id)
    source_id = str(payload["source_id"])
    record_id = str(payload.get("source_record_id", payload.get("record_id", "")))
    result = pipeline.ingest_entity(
        name=name,
        entity_type=entity_type,
        source_id=source_id,
        record_id=record_id,
        external_source=external_source,
        external_id=external_id,
        aliases=list(payload.get("name_aliases") or []),
        content_hash=content_hash,
    )
    if not result.accepted or not identity.knows(result.canonical_id):
        raise SourceAdapterError(f"could not resolve source entity {local_id}: {result.reason}")
    entity_map[local_id] = result.canonical_id
    counters["entity_records_linked"] += 1
    counters["entities_created"] += int(result.is_new)
    counters["identities_reused"] += int(not result.is_new)
    counters["deduplicated_records"] += int(result.replayed)


def _replay_document(
    event: SemanticEvent,
    pipeline: IngestionPipeline,
    counters: Counter[str],
) -> None:
    payload = event.payload
    previous_sequence = pipeline._log.last_sequence
    result = pipeline.register_document(
        uri=str(payload["uri"]),
        title=str(payload["title"]),
        source_system=str(payload["source_system"]),
        content_ref=payload.get("content_ref"),
        record_id=str(payload["source_record_id"]),
        content_hash=str(payload["content_hash"]),
    )
    is_new = result.sequence is not None and result.sequence > previous_sequence
    counters["documents_written"] += int(is_new)
    counters["deduplicated_records"] += int(not is_new)


def _replay_assertion(
    event: SemanticEvent,
    entity_map: dict[str, str],
    identity: IdentityService,
    pipeline: IngestionPipeline,
    counters: Counter[str],
) -> None:
    payload = event.payload
    old_subject_id = str(payload["subject_id"])
    subject_id = entity_map.get(old_subject_id, old_subject_id)
    if not identity.knows(subject_id):
        raise SourceAdapterError(
            f"assertion {event.event_id} has unresolved subject {old_subject_id}"
        )
    object_value = payload["object"]
    object_kind = str(object_value["kind"])
    value = str(object_value["value"])
    if object_kind == "entity":
        value = entity_map.get(value, value)
        if not identity.knows(value):
            raise SourceAdapterError(f"assertion {event.event_id} has unresolved entity object")
    result = pipeline.ingest_assertion(
        subject_id=subject_id,
        predicate_iri=str(payload["predicate_iri"]),
        object_kind=object_kind,
        object_value=value,
        valid_from=str(payload["valid_from"]),
        valid_until=payload.get("valid_until"),
        source_ids=list(payload.get("source_ids") or []),
        evidence=list(payload.get("evidence") or []),
        model_score=payload.get("model_score"),
        evidence_quality=str(payload.get("evidence_quality") or "unknown"),
        idempotency_key=event.idempotency_key
        or _assertion_operation_key(payload, subject_id, value),
        literal_datatype_iri=object_value.get("datatype_iri"),
        literal_language=object_value.get("language"),
    )
    if not result.accepted:
        raise SourceAdapterError(f"assertion {event.event_id} was rejected: {result.reason}")
    counters["assertions_written"] += int(not result.replayed)
    counters["deduplicated_records"] += int(result.replayed)


def _replay_review(
    event: SemanticEvent,
    entity_map: dict[str, str],
    identity: IdentityService,
    pipeline: IngestionPipeline,
    counters: Counter[str],
) -> None:
    payload = event.payload
    candidates = tuple(
        sorted(
            {
                entity_map.get(str(candidate), str(candidate))
                for candidate in payload.get("candidates") or []
                if identity.knows(entity_map.get(str(candidate), str(candidate)))
            }
        )
    )
    canonical_payload = {
        "reference_name": payload.get("reference_name"),
        "external_source": _canonical_external_namespace(payload.get("external_source")),
        "external_id": payload.get("external_id"),
        "entity_type": payload.get("entity_type", ""),
        "candidates": list(candidates),
        "reason": payload.get("reason", ""),
    }
    key = "crawl-review:" + content_sha256(
        json.dumps(canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    previous_sequence = pipeline._log.last_sequence
    result = pipeline.queue_identity_review(
        **canonical_payload,
        idempotency_key=key,
    )
    is_new = (
        result is not None and result.sequence is not None and result.sequence > previous_sequence
    )
    counters["reviews_written"] += int(is_new)
    counters["deduplicated_records"] += int(not is_new)


def _aircraft_mapper(source_id: str) -> OpenSkyMapper | AdsbPositionMapper:
    """Create the mapper used to re-resolve one provider's aircraft records."""
    if source_id == "opensky":
        return OpenSkyMapper(max_records=100_000)
    if source_id in {"adsb-lol", "adsb-fi"}:
        return AdsbPositionMapper(max_records=100_000)
    raise ValueError(f"unsupported aircraft source {source_id!r}")


def _is_stale_aircraft_review(
    event: SemanticEvent,
    replayable_aircraft_ids: set[tuple[str, str]],
) -> bool:
    """Drop a provider-local review only when its raw record resolves it."""
    source = str(event.payload.get("external_source") or "").casefold()
    external_id = str(event.payload.get("external_id") or "").strip().lower()
    return source in LEGACY_ICAO24_NAMESPACES and (source, external_id) in replayable_aircraft_ids


def _canonical_external_namespace(source: str | None) -> str | None:
    """Upgrade historical provider-local ICAO24 keys to the shared namespace."""
    if source is None:
        return None
    normalized = source.strip().casefold()
    return ICAO24_NAMESPACE if normalized in LEGACY_ICAO24_NAMESPACES else source


def _assertion_operation_key(payload: dict[str, Any], subject_id: str, value: str) -> str:
    """Create a deterministic key when an old assertion had no idempotency key."""
    stable = {
        key: payload.get(key)
        for key in (
            "predicate_iri",
            "object",
            "valid_from",
            "valid_until",
            "source_ids",
            "evidence",
            "evidence_quality",
            "supersedes",
        )
    }
    stable["subject_id"] = subject_id
    stable["object_value"] = value
    return "crawl-assertion:" + content_sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _read_archive(
    manifest_path: Path,
    source_id: str,
    contract: SourceContract,
) -> Iterator[RawRecord]:
    """Yield checksum-verified archived records for one aircraft source."""
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SourceAdapterError(
            f"cannot read raw archive manifest {manifest_path}: {exc}"
        ) from exc
    for line_number, line in enumerate(lines, 1):
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or entry.get("source_id") != source_id:
                raise ValueError("source identifier mismatch")
            if entry.get("schema_version") != contract.record_schema:
                raise ValueError("raw archive schema differs from source registry")
            blob = str(entry["blob"])
            if Path(blob).name != blob:
                raise ValueError("invalid blob path")
            body = (manifest_path.parent / blob).read_bytes()
            expected_hash = str(entry["content_hash"])
            if content_sha256(body) != expected_hash:
                raise ValueError("raw archive hash mismatch")
            yield RawRecord(
                source_id=source_id,
                record_id=str(entry["record_id"]),
                source_uri=str(entry["source_uri"]),
                schema_version=str(entry["schema_version"]),
                body=body,
                retrieved_at=str(entry["retrieved_at"]),
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SourceAdapterError(
                f"invalid raw archive entry at {manifest_path}:{line_number}: {exc}"
            ) from exc
