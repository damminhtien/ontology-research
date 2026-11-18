"""Tests for pinned WorldMonitor source discovery and raw crawl behavior."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tools import crawl_worldmonitor_sources as crawler
from tools.crawl_worldmonitor_sources import HttpFetcher, _read_config, crawl_sources

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "worldmonitor_sources.json"
RSS_RESPONSE = b"""<?xml version='1.0' encoding='UTF-8'?>
<rss version='2.0'><channel><item><title>Vietnam maritime update</title>
<link>https://publisher.example/item/1</link><guid>item-1</guid>
<pubDate>Tue, 18 Nov 2026 00:00:00 GMT</pubDate>
<description>Publisher-reported summary.</description></item></channel></rss>"""


def test_worldmonitor_config_covers_the_requested_source_groups() -> None:
    config = _read_config(CONFIG_PATH)
    assert config["upstream"]["commit"] == "b413ca8bc3d8becc87ba5a1849daf2b0adec6afa"
    assert {group["id"] for group in config["groups"]} == {
        "geopolitics-conflict",
        "military-strategic",
        "news-osint",
        "energy-commodities",
        "environment-disasters",
        "aviation-airspace",
        "china-coverage",
        "infrastructure-cyber",
    }
    source_ids = {source["id"] for source in config["sources"]}
    assert {
        "acled",
        "ucdp",
        "gdelt-events",
        "unhcr-population",
        "ocha-reliefweb",
        "polymarket",
        "adsb-lol-military",
        "wingbits",
        "celestrak-active",
        "gpsjam",
        "bbc-world",
        "reuters-world",
        "ap-news",
        "al-jazeera",
        "telegram-reviewed-channels",
        "gie-agsi",
        "eia-petroleum",
        "iea-data",
        "cftc-cot-commodities",
        "usgs-earthquakes",
        "gdacs-events",
        "nasa-eonet",
        "nasa-firms",
        "open-meteo-vietnam-scs",
        "faa-notams",
        "icao-notams",
        "aviationstack-flights",
        "china-nbs",
        "china-safe",
        "china-pboc",
        "taiwan-mnd",
        "shanghai-stock-exchange",
        "shenzhen-stock-exchange",
        "cloudflare-radar",
        "submarine-cable-map",
        "abusech-feodo",
        "abusech-urlhaus",
    } <= source_ids


def test_openwaters_config_is_a_separate_supplemental_vessel_source() -> None:
    config = _read_config(REPO_ROOT / "config" / "openwaters_vessels.json")
    source = config["sources"][0]
    assert source["source_scope"] == "supplemental"
    assert source["fetch_strategy"] == "openwaters_vessel_bbox_tiles"
    assert len(source["bbox_tiles"]) == 6


def test_crawler_archives_raw_feed_and_keeps_claims_as_unverified_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    config = {
        "schema_version": 1,
        "upstream": {"commit": "fixture"},
        "groups": [{"id": "news", "name": "News"}],
        "sources": [
            {
                "id": "example-feed",
                "group": "news",
                "provider": "Example Publisher",
                "kind": "rss",
                "url": "https://publisher.example/rss.xml",
                "access": "public",
                "attribution": "Link to source item.",
            }
        ],
    }
    requests = []

    def fetch(_self, source):
        requests.append(source["id"])
        return RSS_RESPONSE

    monkeypatch.setattr(HttpFetcher, "fetch", fetch)
    run_root = tmp_path / "run"
    manifest = crawl_sources(config, run_root, run_id="fixture")

    assert manifest["summary"]["sources_complete"] == 1
    assert manifest["summary"]["records"] == 1
    raw_path = Path(manifest["source_results"][0]["raw_snapshot"])
    assert raw_path.read_bytes() == RSS_RESPONSE
    assert (
        hashlib.sha256(raw_path.read_bytes()).hexdigest()
        == manifest["source_results"][0]["response_sha256"]
    )
    row = json.loads((run_root / "records.jsonl").read_text(encoding="utf-8"))
    assert row["record_id"] == "item-1"
    assert row["title"] == "Vietnam maritime update"
    assert row["locator"] == "https://publisher.example/item/1"
    assert "assertion" not in row
    assert requests == ["example-feed"]

    crawl_sources(config, run_root, run_id="fixture", resume=True)
    assert requests == ["example-feed"]


def test_crawler_skips_missing_credentials_without_fetching_or_exposing_values(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("WORLD_MONITOR_TEST_TOKEN", raising=False)
    config = {
        "schema_version": 1,
        "upstream": {"commit": "fixture"},
        "groups": [{"id": "energy", "name": "Energy"}],
        "sources": [
            {
                "id": "private-source",
                "group": "energy",
                "provider": "Private API",
                "kind": "json",
                "url": "https://private.example/api",
                "access": "credentialed",
                "credential_env": ["WORLD_MONITOR_TEST_TOKEN"],
                "credential_location": "header",
                "credential_header": "Authorization",
                "attribution": "Provider attribution.",
            }
        ],
    }

    def fail_if_called(_self, _source):
        raise AssertionError("credentialed endpoint must not be called without a key")

    monkeypatch.setattr(HttpFetcher, "fetch", fail_if_called)
    manifest = crawl_sources(config, tmp_path / "run", run_id="credential-test")

    result = manifest["source_results"][0]
    assert result["status"] == "skipped"
    assert result["credential_env"] == ["WORLD_MONITOR_TEST_TOKEN"]
    assert "token-value" not in json.dumps(result)
    assert manifest["summary"]["records"] == 0


def test_gpsjam_crawl_follows_manifest_to_the_latest_daily_observations(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_body = b"date,suspect,num_bad_hexes\n2026-11-17,false,2\n2026-11-18,true,3\n"
    daily_body = (
        b"hex,count_good_aircraft,count_bad_aircraft\n8928308280fffff,5,2\n8928308280bffff,1,4\n"
    )
    config = {
        "schema_version": 1,
        "upstream": {"commit": "fixture"},
        "groups": [{"id": "military", "name": "Military"}],
        "sources": [
            {
                "id": "gpsjam",
                "group": "military",
                "provider": "gpsjam.org",
                "kind": "csv",
                "url": "https://gpsjam.example/data/manifest.csv",
                "daily_url_template": "https://gpsjam.example/data/{date}-h3_4.csv",
                "fetch_strategy": "gpsjam_latest_daily",
                "record_id_fields": ["snapshot_date", "hex"],
                "access": "public",
                "attribution": "Derived observations.",
            }
        ],
    }
    urls = []

    def fetch(_self, source):
        urls.append(source["url"])
        return manifest_body if source["url"].endswith("manifest.csv") else daily_body

    monkeypatch.setattr(HttpFetcher, "fetch", fetch)
    run_root = tmp_path / "gpsjam-run"
    manifest = crawl_sources(config, run_root, run_id="gpsjam")

    result = manifest["source_results"][0]
    rows = [json.loads(line) for line in (run_root / "records.jsonl").read_text().splitlines()]
    assert urls == [
        "https://gpsjam.example/data/manifest.csv",
        "https://gpsjam.example/data/2026-11-18-h3_4.csv",
    ]
    assert result["record_count"] == 2
    assert result["snapshot_date"] == "2026-11-18"
    assert len(result["raw_snapshots"]) == 2
    assert rows[0]["record_id"] == "2026-11-18:8928308280fffff"
    assert rows[0]["raw_record"]["snapshot_date"] == "2026-11-18"
    assert rows[0]["source_uri"].endswith("2026-11-18-h3_4.csv")


def test_unhcr_crawl_pages_and_falls_back_to_latest_year_with_records(
    tmp_path: Path, monkeypatch
) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 18, tzinfo=tz or UTC)

    monkeypatch.setattr(crawler, "datetime", FixedDateTime)
    config = {
        "schema_version": 1,
        "upstream": {"commit": "fixture"},
        "groups": [{"id": "conflict", "name": "Conflict"}],
        "sources": [
            {
                "id": "unhcr",
                "group": "conflict",
                "provider": "UNHCR",
                "kind": "json",
                "url": "https://unhcr.example/population/",
                "fetch_strategy": "unhcr_population_pages",
                "page_size": 2,
                "max_pages": 5,
                "year_lookback": 2,
                "record_path": "items",
                "access": "public",
                "attribution": "Population estimates.",
            }
        ],
    }
    requests = []

    def fetch(_self, source):
        query = parse_qs(urlsplit(source["url"]).query)
        year = query["year"][0]
        page = query["page"][0]
        requests.append((year, page))
        if year == "2026":
            return b'{"items": [], "maxPages": 0}'
        page_items = {
            "1": [{"id": "record-1"}, {"id": "record-2"}],
            "2": [{"id": "record-3"}],
        }[page]
        return json.dumps({"items": page_items, "maxPages": 2}).encode()

    monkeypatch.setattr(HttpFetcher, "fetch", fetch)
    run_root = tmp_path / "unhcr-run"
    manifest = crawl_sources(config, run_root, run_id="unhcr")

    result = manifest["source_results"][0]
    rows = [json.loads(line) for line in (run_root / "records.jsonl").read_text().splitlines()]
    assert requests == [("2026", "1"), ("2025", "1"), ("2025", "2")]
    assert result["data_year"] == 2025
    assert result["record_count"] == 3
    assert len(result["raw_snapshots"]) == 3
    assert {row["raw_record"]["data_year"] for row in rows} == {2025}
    assert {row["raw_record"]["page"] for row in rows} == {1, 2}


def test_html_crawl_uses_scrapling_to_extract_document_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    html_body = (
        b"<html><head><title>Ports &amp; Shipping</title>"
        b"<meta name='description' content='Maritime update for Viet Nam'>"
        b"<meta property='og:title' content='Fallback title'>"
        b"<link rel='canonical' href='https://source.example/report/1'>"
        b"</head><body><h1>Latest report</h1>"
    )
    config = {
        "schema_version": 1,
        "upstream": {"commit": "fixture"},
        "groups": [{"id": "news", "name": "News"}],
        "sources": [
            {
                "id": "html-source",
                "group": "news",
                "provider": "Example Publisher",
                "kind": "html",
                "url": "https://source.example/report/1",
                "access": "public",
                "attribution": "Publisher page.",
            }
        ],
    }
    monkeypatch.setattr(HttpFetcher, "fetch", lambda _self, _source: html_body)
    run_root = tmp_path / "html-run"
    manifest = crawl_sources(config, run_root, run_id="html")

    result = manifest["source_results"][0]
    record = json.loads((run_root / "records.jsonl").read_text().splitlines()[0])
    assert result["record_count"] == 1
    assert record["title"] == "Ports & Shipping"
    assert record["summary"] == "Maritime update for Viet Nam"
    assert record["raw_record"]["canonical_url"] == "https://source.example/report/1"
    assert record["raw_record"]["snapshot_sha256"] == hashlib.sha256(html_body).hexdigest()


def test_http_failures_are_classified_by_upstream_response() -> None:
    assert (
        crawler._failure_status(crawler.CrawlError("HTTP 401", http_status=401)) == "auth_required"
    )
    assert crawler._failure_status(crawler.CrawlError("HTTP 406", http_status=406)) == "blocked"
    assert (
        crawler._failure_status(crawler.CrawlError("HTTP 429", http_status=429)) == "rate_limited"
    )


def test_openwaters_tiles_archive_raw_responses_and_deduplicate_vessel_observations(
    tmp_path: Path, monkeypatch
) -> None:
    first_feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [105.0, 10.0]},
        "properties": {
            "mmsi": 574001234,
            "imo": 9566631,
            "name": "VESSEL ALPHA",
            "seen": "2026-11-18T11:55:47Z",
            "msg_type": "ShipStaticData",
        },
    }
    second_feature = {
        **first_feature,
        "geometry": {"type": "Point", "coordinates": [110.0, 16.0]},
        "properties": {**first_feature["properties"], "mmsi": 574009999, "name": "VESSEL BETA"},
    }
    payloads = {
        "5,98,15,108": {"type": "FeatureCollection", "features": [first_feature]},
        "15,98,25,108": {
            "type": "FeatureCollection",
            "features": [first_feature, second_feature],
        },
    }
    config = {
        "schema_version": 1,
        "upstream": {"provider": "Open Waters AIS"},
        "groups": [{"id": "vessels", "name": "Vessels"}],
        "sources": [
            {
                "id": "openwaters-vessels",
                "group": "vessels",
                "provider": "Open Waters AIS",
                "kind": "json",
                "url": "https://ais.openwaters.example/v1/vessels",
                "record_path": "features",
                "record_id_fields": [
                    "properties.mmsi",
                    "properties.seen",
                    "properties.msg_type",
                ],
                "fetch_strategy": "openwaters_vessel_bbox_tiles",
                "bbox_tiles": [[5, 98, 15, 108], [15, 98, 25, 108]],
                "access": "public",
                "deduplicate_by_source_record_key": True,
                "attribution": "Open Waters AIS.",
            }
        ],
    }
    requests = []

    def fetch(_self, source):
        bbox = parse_qs(urlsplit(source["url"]).query)["bbox"][0]
        requests.append(bbox)
        return json.dumps(payloads[bbox]).encode()

    monkeypatch.setattr(HttpFetcher, "fetch", fetch)
    run_root = tmp_path / "openwaters-run"
    manifest = crawl_sources(config, run_root, run_id="openwaters")

    result = manifest["source_results"][0]
    rows = [json.loads(line) for line in (run_root / "records.jsonl").read_text().splitlines()]
    assert requests == ["5,98,15,108", "15,98,25,108"]
    assert result["record_count"] == 2
    assert result["duplicate_records_removed"] == 1
    assert len(result["raw_snapshots"]) == 2
    assert rows[0]["record_id"] == "574001234:2026-11-18T11:55:47Z:ShipStaticData"
    assert rows[0]["title"] == "VESSEL ALPHA"
    assert rows[0]["published_at"] == "2026-11-18T11:55:47Z"


def test_openwaters_rejects_tiles_over_the_anonymous_area_limit(tmp_path: Path) -> None:
    config = {
        "schema_version": 1,
        "upstream": {"provider": "Open Waters AIS"},
        "groups": [{"id": "vessels", "name": "Vessels"}],
        "sources": [
            {
                "id": "openwaters-vessels",
                "group": "vessels",
                "provider": "Open Waters AIS",
                "kind": "json",
                "url": "https://ais.openwaters.example/v1/vessels",
                "fetch_strategy": "openwaters_vessel_bbox_tiles",
                "bbox_tiles": [[5, 98, 15, 108.1]],
                "access": "public",
                "attribution": "Open Waters AIS.",
            }
        ],
    }
    path = tmp_path / "over_limit.json"
    path.write_text(json.dumps(config))

    try:
        crawler._read_config(path)
    except crawler.CrawlError as exc:
        assert "exceeds the anonymous area limit" in str(exc)
    else:
        raise AssertionError("over-limit Open Waters bbox should be rejected")


def test_openwaters_subdivides_truncated_tiles_and_ignores_partial_parent_rows(
    tmp_path: Path, monkeypatch
) -> None:
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [100.0, 5.0]},
        "properties": {"mmsi": 574001234, "seen": "2026-11-18T11:55:47Z", "msg_type": "Position"},
    }
    parent_feature = {
        **feature,
        "properties": {"mmsi": 574009999, "seen": "2026-11-18T11:55:47Z", "msg_type": "Position"},
    }
    config = {
        "schema_version": 1,
        "upstream": {"provider": "Open Waters AIS"},
        "groups": [{"id": "vessels", "name": "Vessels"}],
        "sources": [
            {
                "id": "openwaters-vessels",
                "group": "vessels",
                "provider": "Open Waters AIS",
                "kind": "json",
                "url": "https://ais.openwaters.example/v1/vessels",
                "record_path": "features",
                "record_id_fields": ["properties.mmsi", "properties.seen", "properties.msg_type"],
                "fetch_strategy": "openwaters_vessel_bbox_tiles",
                "bbox_tiles": [[0, 98, 10, 108]],
                "max_tile_depth": 1,
                "access": "public",
                "deduplicate_by_source_record_key": True,
                "attribution": "Open Waters AIS.",
            }
        ],
    }
    requests = []

    def fetch(_self, source):
        bbox = parse_qs(urlsplit(source["url"]).query)["bbox"][0]
        requests.append(bbox)
        if bbox == "0,98,10,108":
            payload = {"type": "FeatureCollection", "features": [parent_feature], "truncated": True}
        else:
            payload = {"type": "FeatureCollection", "features": [feature]}
        return json.dumps(payload).encode()

    monkeypatch.setattr(HttpFetcher, "fetch", fetch)
    run_root = tmp_path / "openwaters-split-run"
    manifest = crawl_sources(config, run_root, run_id="openwaters-split")

    result = manifest["source_results"][0]
    rows = [json.loads(line) for line in (run_root / "records.jsonl").read_text().splitlines()]
    assert len(requests) == 5
    assert result["status"] == "complete"
    assert result["source_truncated"] is False
    assert result["subdivided_bbox_tiles"] == ["0,98,10,108"]
    assert result["duplicate_records_removed"] == 3
    assert [row["raw_record"]["properties"]["mmsi"] for row in rows] == [574001234]
