r"""Measure real ingestion rates against the live Wikidata endpoint.

Answers two questions with numbers instead of guesses:

1. How big is the full problem? COUNT(*) per mapped Wikidata class + the
   official dump size (Content-Length) + site statistics.
2. How fast/clean is our pipeline on live data? Timed batches through the
   canonical pipeline (identity + SHACL gate + lake write) yield the KPI
   rates (unresolved, no-type, bytes/event) we extrapolate from.

Usage:
    .venv/bin/python tools/measure_wikidata.py \\
        --counts Q43229,Q4830453 \\
        --batches Q43229:1000,Q79913:500 \\
        --delay 3 --out build/wikidata-measurements.json

Never touches the canonical event log or lake: batches run against temp
paths under data/measure/ so production state stays clean.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry import wikidata as wd  # noqa: E402
from foundry.events import EventLog, event_to_dict  # noqa: E402
from foundry.identity import IdentityService  # noqa: E402
from foundry.ingestion import IngestionPipeline  # noqa: E402
from foundry.lake import persist_events  # noqa: E402

DUMP_URL = "https://dumps.wikimedia.org/wikidatawiki/entities/latest-all.json.gz"
STATS_URL = (
    "https://www.wikidata.org/w/api.php?action=query&meta=siteinfo"
    "&siprop=statistics&format=json"
)


def _get_json(url: str, timeout: float = 60.0, retries: int = 2) -> dict:
    """GET a JSON document with polite retries; shared by stats/counts."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(3.0 * attempt)
        req = urllib.request.Request(url, headers={"User-Agent": wd.USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            last = exc
    raise wd.WikidataError(f"GET failed after {retries + 1} attempts: {last}")


def _content_length(url: str, timeout: float = 30.0) -> int | None:
    """HEAD the URL and return Content-Length when present."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": wd.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            value = resp.headers.get("Content-Length")
        return int(value) if value else None
    except OSError:
        return None


def count_class(class_qid: str, timeout: float = 55.0) -> int:
    """COUNT DISTINCT items below the class through P31/P279*."""
    query = (
        f"SELECT (COUNT(DISTINCT ?item) AS ?c) WHERE {{ "
        f"?item wdt:P31/wdt:P279* wd:{class_qid} . }}"
    )
    url = f"{wd.WIKIDATA_ENDPOINT}?{urllib.parse.urlencode({'query': query, 'format': 'json'})}"
    data = _get_json(url, timeout=timeout)
    bindings = data["results"]["bindings"]
    return int(bindings[0]["c"]["value"])


def run_batch(
    class_qid: str,
    limit: int,
    workdir: Path,
    fetcher=None,
) -> dict[str, object]:
    """One timed live batch: fetch -> pipeline -> lake. Returns measurement dict."""
    workdir.mkdir(parents=True, exist_ok=True)
    pipeline = IngestionPipeline(
        identity=IdentityService(),
        log=EventLog(workdir / "events.jsonl"),
        ontology_path=REPO_ROOT / "ontology" / "core" / "core.ttl",
        shapes_path=REPO_ROOT / "shapes" / "core_shapes.ttl",
    )

    t0 = time.perf_counter()
    fetch_kwargs = {"fetcher": fetcher} if fetcher is not None else {}
    records = wd.fetch_entities(class_qid=class_qid, limit=limit, **fetch_kwargs)
    fetch_seconds = time.perf_counter() - t0

    t1 = time.perf_counter()
    stats, _receipts, events = wd.ingest_records(pipeline, records)
    ingest_seconds = time.perf_counter() - t1

    lake_root = workdir / "lake"
    t2 = time.perf_counter()
    files = persist_events(events, lake_root)
    lake_seconds = time.perf_counter() - t2
    lake_bytes = sum(f.bytes for f in files)
    json_bytes = sum(len(json.dumps(event_to_dict(e), ensure_ascii=False)) for e in events)

    d = stats.as_dict()
    return {
        "class": class_qid,
        "limit": limit,
        "fetch_seconds": round(fetch_seconds, 2),
        "records": len(records),
        "mapped": d["total"],
        "mapped_rate": round(d["total"] / len(records), 4) if records else 0.0,
        "accepted": d["accepted"],
        "new_entities": d["new_entities"],
        "merged": d["merged"],
        "rejected": d["rejected"],
        "skipped_no_type": d["skipped_no_type"],
        "unresolved_rate": d["unresolved_rate"],
        "rejection_reasons": d["rejection_reasons"],
        "ingest_seconds": round(ingest_seconds, 3),
        "events": len(events),
        "ingest_events_per_sec": round(len(events) / ingest_seconds, 1) if ingest_seconds else 0.0,
        "lake_seconds": round(lake_seconds, 3),
        "lake_bytes": lake_bytes,
        "lake_bytes_per_event": round(lake_bytes / len(events), 1) if events else 0.0,
        "json_bytes_per_event": round(json_bytes / len(events), 1) if events else 0.0,
    }


def main() -> int:
    """Run counts + timed batches, write the measurement JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", default="", help="comma-separated class QIDs to COUNT")
    parser.add_argument("--batches", default="", help="comma-separated QID:limit pairs")
    parser.add_argument("--delay", type=float, default=3.0, help="seconds between requests")
    parser.add_argument("--out", default="build/wikidata-measurements.json")
    args = parser.parse_args()

    report: dict[str, object] = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    stats = _get_json(STATS_URL, timeout=20.0)["query"]["statistics"]
    report["wikidata_total_items"] = stats["pages"]
    dump_bytes = _content_length(DUMP_URL)
    report["dump_compressed_bytes"] = dump_bytes
    print(f"wikidata items: {stats['pages']:,}; dump compressed: {dump_bytes:,} bytes")

    counts: dict[str, object] = {}
    for qid in [q for q in args.counts.split(",") if q]:
        try:
            t0 = time.perf_counter()
            counts[qid] = count_class(qid)
            print(f"count {qid}: {counts[qid]:,} ({time.perf_counter() - t0:.1f}s)")
        except wd.WikidataError as exc:
            counts[qid] = {"error": str(exc)[:120]}
            print(f"count {qid}: FAILED ({exc})")
        time.sleep(args.delay)
    report["class_counts"] = counts

    batches: list[dict[str, object]] = []
    for pair in [p for p in args.batches.split(",") if p]:
        qid, _, lim = pair.partition(":")
        try:
            result = run_batch(qid, int(lim or 500), REPO_ROOT / "data" / "measure")
        except wd.WikidataError as exc:
            result = {"class": qid, "error": str(exc)[:120]}
        batches.append(result)
        print(json.dumps(result, ensure_ascii=False))
        time.sleep(args.delay)
    report["batches"] = batches

    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"report written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
