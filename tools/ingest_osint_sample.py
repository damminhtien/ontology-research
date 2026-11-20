"""Run a bounded Wikidata, GLEIF and Wikipedia ingestion into an isolated local log.

The sample is deliberately small and never defaults to the production event
log. All raw snapshots, checkpoints and output events are written under the
external crawl root configured by ``FOUNDRY_CRAWL_ROOT`` or ``FOUNDRY_DATA_ROOT``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EVENT_TYPE_REVIEW_QUEUED, EventLog
from foundry.ingestion import IngestionPipeline
from foundry.merge import rebuild_identity
from foundry.paths import crawl_path
from foundry.projector import Projector
from foundry.provenance import stable_source_record_key
from foundry.public_sources import (
    CandidateFactExtractor,
    GleifConnector,
    GleifEntityMapper,
    WikidataConnector,
    WikidataEntityMapper,
    WikipediaConnector,
    WikipediaDocumentMapper,
    ingest_entity_record,
)
from foundry.query_service import QueryService
from foundry.readmodel import ReadModel
from foundry.source_adapters import (
    CheckpointStore,
    JsonHttpClient,
    RawArchive,
    SourceAdapterError,
    load_source_contracts,
    run_connector_page,
)


def main() -> int:
    """Fetch, archive, map, validate, commit, project and query one bounded sample."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        help="isolated run directory name (letters, digits, dash and underscore)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=crawl_path() / "osint-mvp",
        help="external crawl-run root; defaults outside the repository",
    )
    parser.add_argument(
        "--source-registry",
        type=Path,
        default=REPO_ROOT / "config" / "source_registry.json",
        help="source contract registry",
    )
    parser.add_argument(
        "--qids",
        default="Q24039192,Q9531,Q2283",
        help="comma-separated bounded Wikidata entity IDs",
    )
    parser.add_argument("--lei", default="INR2EJN1ERAN0W5ZP974", help="GLEIF LEI to cross-check")
    parser.add_argument("--wiki-title", default="BBC Studios", help="Wikipedia article title")
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("--run-id may contain only letters, digits, dash and underscore")
    qids = tuple(item.strip() for item in args.qids.split(",") if item.strip())
    run_root = args.root.expanduser() / args.run_id
    run_root.mkdir(parents=True, exist_ok=True)
    log = EventLog(run_root / "events.jsonl")
    try:
        contracts = load_source_contracts(args.source_registry)
        identity = rebuild_identity(log)
        pipeline = IngestionPipeline(
            identity=identity,
            log=log,
            ontology_path=REPO_ROOT / "ontology" / "middle" / "organization.ttl",
            shapes_path=REPO_ROOT / "shapes" / "core_shapes.ttl",
        )
        http = JsonHttpClient(
            user_agent="ontology-research-bot/0.1 (+https://github.com/damminhtien/ontology-research)"
        )
        archive = RawArchive(run_root / "raw")
        checkpoints = CheckpointStore(run_root / "checkpoints")

        def ingest_entities(_raw, entities, _archived, source_key: str) -> None:
            for entity in entities:
                if entity.source_id != source_key:
                    raise SourceAdapterError("entity mapper returned a mismatched source")
                result = ingest_entity_record(pipeline, identity, log, entity)
                if not result.accepted and (
                    result.event is None or result.event.event_type != EVENT_TYPE_REVIEW_QUEUED
                ):
                    raise SourceAdapterError(
                        f"entity {entity.record_id} was rejected without durable review"
                    )

        def run_to_completion(connector, mapper, handle) -> None:
            while True:
                page = run_connector_page(
                    connector,
                    mapper,
                    archive=archive,
                    checkpoints=checkpoints,
                    handle=handle,
                )
                if page is None or page.complete:
                    return

        wikidata = WikidataConnector(qids, contract=contracts["wikidata"], http=http)
        run_to_completion(
            wikidata,
            WikidataEntityMapper(),
            lambda raw, entities, archived: ingest_entities(raw, entities, archived, "wikidata"),
        )

        gleif = GleifConnector(args.lei, contract=contracts["gleif"], http=http)
        run_to_completion(
            gleif,
            GleifEntityMapper(),
            lambda raw, entities, archived: ingest_entities(raw, entities, archived, "gleif"),
        )

        wikipedia = WikipediaConnector(
            args.wiki_title, contract=contracts["wikipedia-en"], http=http
        )

        def ingest_documents(_raw, documents, _archived) -> None:
            for document in documents:
                result = pipeline.ingest_document(
                    uri=document.source_uri,
                    title=document.title,
                    source_system=document.source_id,
                    text=document.text,
                    content_ref=document.source_uri,
                    record_id=document.record_id,
                    extractor=CandidateFactExtractor(document.candidate_facts),
                )
                handled = result.asserted + result.queued + result.skipped
                if handled != result.candidates:
                    raise SourceAdapterError(
                        f"document {document.record_id} has uncommitted mapper proposals"
                    )

        run_to_completion(
            wikipedia,
            WikipediaDocumentMapper(),
            ingest_documents,
        )

        document_mapper = WikipediaDocumentMapper()
        document = document_mapper.map(
            archive.read_latest(
                "wikipedia-en",
                f"article:{args.wiki_title.casefold()}",
                contracts["wikipedia-en"],
            )
        )[0]
        document_key = stable_source_record_key(document.source_id, document.record_id)
        document_event = next(
            (
                event
                for event in log.iter_all()
                if event.event_type == "DocumentRegistered"
                and event.payload.get("source_record_key") == document_key
                and event.payload.get("content_hash") == document.content_hash
            ),
            None,
        )
        if document_event is None:
            raise SourceAdapterError("archived document has no committed DocumentRegistered event")
        document_id = document_event.payload["document_id"]
        assertion_events = [
            event
            for event in log.iter_all()
            if event.event_type == "AssertionMade"
            and document_id in event.payload.get("source_ids", [])
        ]
        if len(assertion_events) != len(document.candidate_facts):
            raise SourceAdapterError(
                f"expected {len(document.candidate_facts)} committed document assertions; "
                f"found {len(assertion_events)}"
            )
        assertion_event = assertion_events[0]

        microsoft = identity.lookup(external_source="wikidata", external_id="Q2283")
        lei_match = identity.lookup(
            external_source="gleif-lei", external_id=args.lei, entity_type="Organization"
        )
        if microsoft.method != "external_id" or microsoft.canonical_id != lei_match.canonical_id:
            raise SourceAdapterError("Wikidata and GLEIF did not resolve to one canonical entity")
        read_model = ReadModel()
        projector = Projector(read_model)
        projection_stats = projector.replay(log.read_all())
        queries = QueryService(read_model)
        assertion_view = queries.assertion_provenance(assertion_event.payload["assertion_id"])
        if assertion_view is None:
            raise SourceAdapterError("projected assertion is not queryable")
        if not assertion_view.get("evidence"):
            raise SourceAdapterError("projected assertion has no document evidence")

        summary = {
            "run_id": args.run_id,
            "run_root": str(run_root.resolve()),
            "event_log": str((run_root / "events.jsonl").resolve()),
            "sources": {
                "wikidata": {
                    "mapped": checkpoints.load("wikidata")["mapped_records"],
                    "qids": list(qids),
                },
                "gleif": {
                    "mapped": checkpoints.load("gleif")["mapped_records"],
                    "lei": args.lei,
                },
                "wikipedia_en": {
                    "title": document.title,
                    "revision_id": document.revision_id,
                    "revision_timestamp": document.revision_timestamp,
                    "content_hash": document.content_hash,
                    "mapped": checkpoints.load("wikipedia-en")["mapped_records"],
                },
            },
            "identity_reconciliation": {
                "wikidata_qid": "Q2283",
                "gleif_lei": args.lei,
                "canonical_entity_id": microsoft.canonical_id,
                "same_canonical_entity": True,
            },
            "document_assertion": {
                "document_id": document_id,
                "assertion_id": assertion_event.payload["assertion_id"],
                "valid_from": assertion_event.payload["valid_from"],
                "recorded_at": assertion_event.payload["recorded_at"],
                "evidence": assertion_view.get("evidence"),
            },
            "projection": {
                "applied": projection_stats.applied,
                "duplicates": projection_stats.duplicates,
                "checkpoint_sequence": read_model.checkpoint_sequence,
            },
        }
        serialized_summary = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
        temporary_summary = run_root / "summary.json.tmp"
        temporary_summary.write_text(serialized_summary + "\n", encoding="utf-8")
        temporary_summary.replace(run_root / "summary.json")
        print(serialized_summary)
    except (KeyError, ValueError, SourceAdapterError) as exc:
        print(f"OSINT ingestion failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
