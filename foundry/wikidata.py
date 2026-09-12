"""Wikidata ingestion adapter (first real external source).

Fetch path (all structured, CC0-licensed, RDF-native):

    Wikidata SPARQL endpoint -> normalized records -> IngestionPipeline
    (identity resolution via external_id, then SHACL gate) -> event log
    -> columnar lake (LakeWriter)

Wikidata QIDs are used as ``external_id`` (confidence 1.0 exact match), which
exercises the identity service's strongest resolution path against data we do
not control. The module never writes to the event log itself — every record
goes through :class:`foundry.ingestion.IngestionPipeline`, so SHACL and
identity gates stay authoritative.

This module is intentionally side-effect free: ``fetch_*`` only performs HTTP
GETs and returns plain records; callers decide what to ingest.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from foundry.events import SemanticEvent, utc_now_iso
from foundry.ingestion import IngestionPipeline, IngestResult
from foundry.reference import ReferenceRecord

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "ontology-research/0.1 (https://github.com/damminhtien/ontology-research)"

# Wikidata class -> core entity type. Only types the ingestion pipeline
# accepts are listed here; anything else is skipped with a structured reason.
QID_TO_ENTITY_TYPE: dict[str, str] = {
    "Q5": "Person",  # human
    "Q43229": "Organization",  # organization
    "Q4830453": "Organization",  # business
    "Q79913": "Organization",  # non-governmental organization
    "Q176799": "Organization",  # military unit
    "Q3918": "Organization",  # university
    "Q16917": "Facility",  # hospital
    "Q41176": "Facility",  # museum building
    "Q33506": "Organization",  # museum
    "Q23691": "Facility",  # airport
}

# The entity type is derived from class_qid itself: every returned item is in
# the P279* closure of the queried class by construction, so the expensive
# ``?item wdt:P31/wdt:P279* ?type`` join is unnecessary. Keeping it made LIMIT
# burn on duplicate ancestor-type rows (3000 rows -> 32 entities) and made
# large closure queries time out on the endpoint (HTTP 504).
DEFAULT_QUERY_TEMPLATE = """
SELECT ?item ?labelVi ?labelEn WHERE {
  ?item wdt:P31/wdt:P279* wd:%(class)s .
  ?item rdfs:label ?labelVi .
  FILTER(LANG(?labelVi) = "vi")
  OPTIONAL { ?item rdfs:label ?labelEn . FILTER(LANG(?labelEn) = "en") }
}
LIMIT %(limit)d
"""


class WikidataError(RuntimeError):
    """Raised when the Wikidata SPARQL endpoint fails or returns bad data."""


@dataclass(frozen=True)
class WikidataRecord:
    """One normalized Wikidata entity row."""

    qid: str
    name: str
    entity_type: str | None
    aliases: tuple[str, ...] = ()
    type_qids: tuple[str, ...] = ()


@dataclass
class IngestStats:
    """Aggregate outcome of one ingestion run — the KPI feed."""

    total: int = 0
    skipped_no_type: int = 0
    accepted: int = 0
    new_entities: int = 0
    merged: int = 0
    rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def unresolved_rate(self) -> float:
        """Fraction of processed records sent to review instead of resolving."""
        return self.rejected / self.total if self.total else 0.0

    def as_dict(self) -> dict[str, object]:
        """JSON-serializable stats block for logs and KPI feeds."""
        return {
            "total": self.total,
            "skipped_no_type": self.skipped_no_type,
            "accepted": self.accepted,
            "new_entities": self.new_entities,
            "merged": self.merged,
            "rejected": self.rejected,
            "rejection_reasons": dict(self.rejection_reasons),
            "unresolved_rate": round(self.unresolved_rate, 4),
        }


def _http_get_json(url: str, *, timeout: float) -> dict[str, object]:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise WikidataError(f"SPARQL endpoint unreachable: {exc}") from exc
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WikidataError(f"endpoint returned non-JSON body: {exc}") from exc
    if not isinstance(data, dict) or "results" not in data:
        raise WikidataError("endpoint response is not a SPARQL JSON result")
    return data


def map_type(type_qids: tuple[str, ...]) -> str | None:
    """Map the first recognized Wikidata class to a core entity type."""
    for qid in type_qids:
        if qid in QID_TO_ENTITY_TYPE:
            return QID_TO_ENTITY_TYPE[qid]
    return None


def _fetch_with_retry(
    url: str,
    *,
    timeout: float,
    retries: int,
    backoff: float,
    fetcher: Callable[[str, float], dict[str, object]],
) -> dict[str, object]:
    """GET a SPARQL JSON result with bounded exponential backoff."""
    data: dict[str, object] | None = None
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(backoff * attempt)
        try:
            data = fetcher(url, timeout=timeout)
            last_error = None
            break
        except WikidataError as exc:
            last_error = exc
    if data is None:
        raise WikidataError(f"query failed after {retries + 1} attempts: {last_error}")
    return data


def _bindings(data: dict[str, object]) -> list[dict[str, object]]:
    results = data.get("results")
    return results.get("bindings", []) if isinstance(results, dict) else []


# Cursor paging (architecture §4.6): the next page starts strictly after the
# last item of the previous page (a QID cursor), never a LIMIT window that
# re-scans and re-fetches overlapping items. ORDER BY ?item matches the
# STR(?item) cursor comparison, so pages tile the closure without overlap.
CURSOR_QUERY_TEMPLATE = """
SELECT ?item ?labelVi ?labelEn WHERE {
  ?item wdt:P31/wdt:P279* wd:%(class)s .
  ?item rdfs:label ?labelVi .
  FILTER(LANG(?labelVi) = "vi")
  FILTER(STR(?item) > "%(cursor)s")
  OPTIONAL { ?item rdfs:label ?labelEn . FILTER(LANG(?labelEn) = "en") }
}
ORDER BY ?item
LIMIT %(limit)d
"""

WIKIDATA_ENTITY_IRI = "http://www.wikidata.org/entity/"


def iter_entities(
    *,
    class_qid: str = "Q43229",
    page_size: int = 2000,
    max_records: int | None = None,
    endpoint: str = WIKIDATA_ENDPOINT,
    timeout: float = 60.0,
    retries: int = 2,
    backoff: float = 5.0,
    fetcher: Callable[[str, float], dict[str, object]] = _http_get_json,
) -> Iterator[ReferenceRecord]:
    """Page through the class closure by QID cursor; yields typed lane rows.

    One row per item per page (the template joins no types, and an item has
    at most one vi label), so pages tile the closure exactly: the cursor is
    the last item IRI and each page continues strictly after it. Stops at a
    short page (closure exhausted) or after ``max_records`` rows.
    ``fetcher`` is injectable so tests never touch the network.
    """
    cursor = ""
    yielded = 0
    while max_records is None or yielded < max_records:
        query = CURSOR_QUERY_TEMPLATE % {"class": class_qid, "cursor": cursor, "limit": page_size}
        url = f"{endpoint}?{urllib.parse.urlencode({'query': query, 'format': 'json'})}"
        data = _fetch_with_retry(
            url, timeout=timeout, retries=retries, backoff=backoff, fetcher=fetcher
        )
        fetched_at = utc_now_iso()
        page: list[ReferenceRecord] = []
        for binding_ in _bindings(data):
            item_iri = binding_.get("item", {}).get("value", "")
            qid = item_iri.rsplit("/", 1)[-1]
            if not qid.startswith("Q"):
                continue
            name_vi = (binding_.get("labelVi", {}).get("value") or "").strip()
            name_en = (binding_.get("labelEn", {}).get("value") or "").strip()
            if not name_vi and not name_en:
                continue  # unusable surface form; skipped, never silently wrong
            page.append(
                ReferenceRecord(
                    qid=qid,
                    name_vi=name_vi,
                    name_en=name_en,
                    type_qids=(),
                    class_qid=class_qid,
                    fetched_at=fetched_at,
                )
            )
        if not page:
            return
        for row in page:
            if max_records is not None and yielded >= max_records:
                return
            yield row
            yielded += 1
        cursor = WIKIDATA_ENTITY_IRI + page[-1].qid
        if len(page) < page_size:
            return


def records_from_reference(rows: Iterable[ReferenceRecord]) -> list[WikidataRecord]:
    """Convert lane rows into ingestable records (vi preferred, en as alias).

    The lane is reference data, never truth: conversion drops rows without a
    usable surface name and derives the entity type from the row's declared
    classes, falling back to the queried root class (same rule as the live
    fetch path).
    """
    records: list[WikidataRecord] = []
    for row in rows:
        name = row.name_vi.strip() or row.name_en.strip()
        if not name:
            continue
        en = row.name_en.strip()
        aliases = tuple(a for a in (en,) if a and a != name)
        records.append(
            WikidataRecord(
                qid=row.qid,
                name=name,
                entity_type=map_type(row.type_qids) or QID_TO_ENTITY_TYPE.get(row.class_qid),
                aliases=aliases,
                type_qids=row.type_qids,
            )
        )
    return records


def fetch_entities(
    *,
    class_qid: str = "Q43229",
    limit: int = 500,
    endpoint: str = WIKIDATA_ENDPOINT,
    timeout: float = 60.0,
    retries: int = 2,
    backoff: float = 5.0,
    fetcher: Callable[[str, float], dict[str, object]] = _http_get_json,
) -> list[WikidataRecord]:
    """Run the P31 lookup query and normalize the bindings into records.

    ``fetcher`` is injectable so tests never touch the network.
    """
    query = DEFAULT_QUERY_TEMPLATE % {"class": class_qid, "limit": limit}
    url = f"{endpoint}?{urllib.parse.urlencode({'query': query, 'format': 'json'})}"

    data: dict[str, object] | None = None
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(backoff * attempt)
        try:
            data = fetcher(url, timeout=timeout)
            last_error = None
            break
        except WikidataError as exc:
            last_error = exc
    if data is None:
        raise WikidataError(f"query failed after {retries + 1} attempts: {last_error}")

    results = data["results"]
    bindings = results.get("bindings", []) if isinstance(results, dict) else []
    records: dict[str, WikidataRecord] = {}
    # Every returned item is inside the queried class's P279* closure, so the
    # class's own mapped core type is a sound fallback when a row carries no
    # explicit ancestor type (the template no longer joins types at all).
    fallback_type = QID_TO_ENTITY_TYPE.get(class_qid)
    for binding in bindings:
        qid = binding.get("item", {}).get("value", "").rsplit("/", 1)[-1]
        if not qid.startswith("Q"):
            continue
        name = (binding.get("labelVi", {}).get("value") or "").strip()
        en = (binding.get("labelEn", {}).get("value") or "").strip()
        name = name or en
        if not name:
            continue
        aliases = tuple(a for a in (en,) if a and a != name)
        type_qid = binding.get("type", {}).get("value", "").rsplit("/", 1)[-1]
        type_qids = (type_qid,) if type_qid else ()
        existing = records.get(qid)
        if existing is None:
            records[qid] = WikidataRecord(
                qid=qid,
                name=name,
                entity_type=map_type(type_qids) or fallback_type,
                aliases=aliases,
                type_qids=type_qids,
            )
        else:  # multiple P31 rows per item: merge classes + aliases, keep best type
            merged_types = tuple(dict.fromkeys((*existing.type_qids, *type_qids)))
            records[qid] = WikidataRecord(
                qid=qid,
                name=existing.name,
                entity_type=map_type(merged_types) or fallback_type,
                aliases=tuple(dict.fromkeys((*existing.aliases, *aliases))),
                type_qids=merged_types,
            )
    return list(records.values())


def ingest_records(
    pipeline: IngestionPipeline, records: Iterator[WikidataRecord] | list[WikidataRecord]
) -> tuple[IngestStats, list[IngestResult], list[SemanticEvent]]:
    """Ingest normalized records through the validated pipeline.

    Returns ``(stats, receipts, accepted_events)`` — accepted events are what
    callers may project or write to the lake. Rejections are counted with
    reasons, never dropped silently.
    """
    stats = IngestStats()
    receipts: list[IngestResult] = []
    events: list[SemanticEvent] = []
    for record in records:
        stats.total += 1
        if record.entity_type is None:
            stats.skipped_no_type += 1
            continue
        result = pipeline.ingest_entity(
            name=record.name,
            entity_type=record.entity_type,
            source_id=f"wikidata:{record.qid}",
            external_source="wikidata",
            external_id=record.qid,
            aliases=list(record.aliases) or None,
        )
        receipts.append(result)
        if result.accepted:
            stats.accepted += 1
            if result.is_new:
                stats.new_entities += 1
            else:
                stats.merged += 1
            if result.event is not None:
                events.append(result.event)
        else:
            stats.rejected += 1
            reason = result.reason.split(";")[0][:80]
            stats.rejection_reasons[reason] = stats.rejection_reasons.get(reason, 0) + 1
            if result.event is not None:
                # a durable ResolutionReviewQueued fact rides along with the
                # rejection so the review queue reaches the lake too
                events.append(result.event)
    return stats, receipts, events
