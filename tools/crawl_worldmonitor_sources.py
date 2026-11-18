#!/usr/bin/env python3
"""Fetch the pinned WorldMonitor source set into an immutable local archive."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "worldmonitor_sources.json"
DEFAULT_ROOT = Path("/Users/macbook/Data/ontology-research/data/worldmonitor")
USER_AGENT = (
    "semantic-foundry-worldmonitor-crawl/1.0 (+https://github.com/damminhtien/ontology-research)"
)
MAX_RESPONSE_BYTES = 50 * 1024 * 1024
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


class CrawlError(RuntimeError):
    """One source request or parse operation failed safely."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        """Keep a sanitized failure message and optional HTTP status."""
        super().__init__(message)
        self.http_status = http_status


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CrawlError(f"cannot read source config: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CrawlError("source config must use schema_version 1")
    groups = payload.get("groups")
    sources = payload.get("sources")
    if not isinstance(groups, list) or not isinstance(sources, list):
        raise CrawlError("source config needs groups and sources lists")
    group_ids = {group.get("id") for group in groups if isinstance(group, dict)}
    source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise CrawlError("every source config entry must be an object")
        source_id = source.get("id")
        if (
            not isinstance(source_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", source_id)
            or source_id in source_ids
        ):
            raise CrawlError(f"invalid or duplicate source id: {source_id!r}")
        source_ids.add(source_id)
        if source.get("group") not in group_ids:
            raise CrawlError(f"{source_id} references an unknown source group")
        if source.get("kind") not in {"rss", "json", "csv", "html"}:
            raise CrawlError(f"{source_id} has an unsupported kind")
        if source.get("fetch_strategy") not in {
            None,
            "gpsjam_latest_daily",
            "unhcr_population_pages",
        }:
            raise CrawlError(f"{source_id} has an unsupported fetch strategy")
        if source.get("fetch_strategy") == "gpsjam_latest_daily" and not (
            isinstance(source.get("daily_url_template"), str)
            and "{date}" in source["daily_url_template"]
        ):
            raise CrawlError(f"{source_id} needs a daily URL template with {{date}}")
        if source.get("fetch_strategy") == "unhcr_population_pages":
            for key, default in (("page_size", 10_000), ("max_pages", 25)):
                value = source.get(key, default)
                if not isinstance(value, int) or value < 1:
                    raise CrawlError(f"{source_id} has an invalid {key}")
            year_lookback = source.get("year_lookback", 2)
            if not isinstance(year_lookback, int) or year_lookback < 0:
                raise CrawlError(f"{source_id} has an invalid year_lookback")
        record_id_fields = source.get("record_id_fields", [])
        if not isinstance(record_id_fields, list) or any(
            not isinstance(value, str) or not value for value in record_id_fields
        ):
            raise CrawlError(f"{source_id} has invalid record_id_fields")
        minimum_interval = source.get("minimum_interval_seconds")
        if minimum_interval is not None and (
            not isinstance(minimum_interval, (int, float)) or minimum_interval < 0
        ):
            raise CrawlError(f"{source_id} has an invalid minimum request interval")
        if source.get("access") == "public":
            url = source.get("url")
            parsed = urllib.parse.urlparse(url) if isinstance(url, str) else None
            if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise CrawlError(f"public source {source_id} needs an HTTP(S) URL")
    return payload


def _credential_envs(source: Mapping[str, Any]) -> tuple[str, ...]:
    value = source.get("credential_env", [])
    if isinstance(value, str):
        return (value,)
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def _credential_values(source: Mapping[str, Any]) -> dict[str, str]:
    return {
        name: os.environ[name].strip()
        for name in _credential_envs(source)
        if os.environ.get(name, "").strip()
    }


def _request_url(source: Mapping[str, Any]) -> str:
    url = str(source["url"])
    return url.replace("{date}", datetime.now(UTC).date().isoformat())


def _source_fingerprint(source: Mapping[str, Any]) -> str:
    safe_source = {key: value for key, value in source.items() if key != "credential_env"}
    return _sha256(_canonical_json(safe_source))


class HttpFetcher:
    """Sequential paced HTTP client with bounded retries and response size."""

    def __init__(self, minimum_interval: float = 1.0, retries: int = 2) -> None:
        """Set per-host pacing and the maximum number of retry attempts."""
        self.minimum_interval = minimum_interval
        self.retries = retries
        self._last_request: dict[str, float] = {}

    def fetch(self, source: Mapping[str, Any]) -> bytes:
        """Fetch one configured source body without logging credential values."""
        url = _request_url(source)
        credentials = _credential_values(source)
        credential_envs = _credential_envs(source)
        if credential_envs and len(credentials) != len(credential_envs):
            raise CrawlError("required credential is unset")

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/json, application/rss+xml, application/xml, text/csv, text/html, */*"
            ),
        }
        credential_location = source.get("credential_location")
        if credential_location == "header" and credentials:
            key = credential_envs[0]
            header_name = str(source.get("credential_header", "Authorization"))
            prefix = str(source.get("credential_prefix", ""))
            headers[header_name] = f"{prefix}{credentials[key]}"
        elif credential_location == "query" and credentials:
            key = credential_envs[0]
            query = urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True)
            query.append((str(source.get("credential_parameter", "key")), credentials[key]))
            parts = urllib.parse.urlsplit(url)
            url = urllib.parse.urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    urllib.parse.urlencode(query),
                    parts.fragment,
                )
            )
        elif "{credential}" in url and credentials:
            url = url.replace(
                "{credential}", urllib.parse.quote(credentials[credential_envs[0]], safe="")
            )

        method = str(source.get("method", "GET")).upper()
        body = b"{}" if method == "POST" else None
        if method not in {"GET", "POST"}:
            raise CrawlError(f"unsupported HTTP method {method}")
        if body is not None:
            headers["Content-Type"] = "application/json"
        host = urllib.parse.urlsplit(url).netloc
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            minimum_interval = max(
                self.minimum_interval,
                float(source.get("minimum_interval_seconds", self.minimum_interval)),
            )
            wait = minimum_interval - (time.monotonic() - self._last_request.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self._last_request[host] = time.monotonic()
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise CrawlError(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
                    return payload
            except urllib.error.HTTPError as exc:
                if exc.code not in TRANSIENT_HTTP_STATUSES or attempt == self.retries:
                    raise CrawlError(f"HTTP {exc.code}", http_status=exc.code) from None
                delay = _retry_after_seconds(exc.headers.get("Retry-After"))
                time.sleep(delay if delay is not None else 2**attempt)
                last_error = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(2**attempt)
        raise CrawlError(
            f"request failed after {self.retries + 1} attempts ({type(last_error).__name__})"
        )


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return min(60.0, max(0.0, float(value)))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return min(60.0, max(0.0, (retry_at - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def _response(
    url: str,
    body: bytes,
    *,
    include_records: bool = True,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "url": url,
        "body": body,
        "include_records": include_records,
        "context": dict(context or {}),
    }


def _url_with_params(url: str, values: Mapping[str, str]) -> str:
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update(values)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def _gpsjam_latest_date(body: bytes) -> str:
    try:
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"), newline="")))
    except UnicodeDecodeError as exc:
        raise CrawlError("GPSJam manifest is not UTF-8") from exc
    if len(rows) < 2 or not rows[0] or rows[0][0].strip().casefold() != "date":
        raise CrawlError("GPSJam manifest has no date rows")
    value = rows[-1][0].strip()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise CrawlError("GPSJam manifest tail has an invalid date") from exc
    if parsed.isoformat() != value:
        raise CrawlError("GPSJam manifest tail has an invalid date")
    return value


def _gpsjam_daily_is_valid(body: bytes) -> bool:
    try:
        rows = csv.reader(io.StringIO(body.decode("utf-8-sig"), newline=""))
        headers = {value.strip() for value in next(rows, [])}
    except UnicodeDecodeError:
        return False
    return {"hex", "count_good_aircraft", "count_bad_aircraft"} <= headers


def _fetch_unhcr_pages(
    fetcher: HttpFetcher, source: Mapping[str, Any], *, current_year: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    page_size = int(source.get("page_size", 10_000))
    max_pages = int(source.get("max_pages", 25))
    year_lookback = int(source.get("year_lookback", 2))
    responses: list[dict[str, Any]] = []
    fallback_http_statuses: list[dict[str, int]] = []

    for year in range(current_year, current_year - year_lookback - 1, -1):
        year_responses: list[dict[str, Any]] = []
        item_count = 0
        truncated = False
        try:
            for page in range(1, max_pages + 1):
                url = _url_with_params(
                    str(source["url"]),
                    {
                        "year": str(year),
                        "limit": str(page_size),
                        "page": str(page),
                        "coo_all": "true",
                        "coa_all": "true",
                    },
                )
                body = fetcher.fetch({**source, "url": url})
                try:
                    payload = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CrawlError("UNHCR JSON parse error") from exc
                if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                    raise CrawlError("UNHCR response has no items list")
                page_items = payload["items"]
                if any(
                    not isinstance(item, (dict, list, str, int, float, bool)) for item in page_items
                ):
                    raise CrawlError("UNHCR page contains an unsupported record")
                year_responses.append(
                    _response(
                        url,
                        body,
                        include_records=bool(page_items),
                        context={"data_year": year, "page": page},
                    )
                )
                item_count += len(page_items)
                if not page_items:
                    break

                advertised_pages = payload.get("maxPages")
                try:
                    page_count = int(advertised_pages)
                except (TypeError, ValueError):
                    page_count = 0
                if page_count > 0:
                    if page >= page_count:
                        break
                    if page == max_pages:
                        truncated = page_count > max_pages
                elif len(page_items) < page_size:
                    break

            responses.extend(year_responses)
            if item_count:
                return responses, {
                    "data_year": year,
                    "pagination_truncated": truncated,
                    "fallback_http_statuses": fallback_http_statuses,
                }
        except CrawlError as exc:
            if exc.http_status not in {404, 410}:
                raise
            fallback_http_statuses.append({"year": year, "http_status": exc.http_status})

    raise CrawlError("UNHCR returned no records for the current year or previous configured years")


def _fetch_source_responses(
    fetcher: HttpFetcher, source: Mapping[str, Any], *, current_year: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    strategy = source.get("fetch_strategy")
    if strategy == "gpsjam_latest_daily":
        manifest_url = str(source["url"])
        manifest_body = fetcher.fetch(source)
        snapshot_date = _gpsjam_latest_date(manifest_body)
        daily_url = str(source["daily_url_template"]).replace("{date}", snapshot_date)
        daily_body = fetcher.fetch({**source, "url": daily_url})
        if not _gpsjam_daily_is_valid(daily_body):
            raise CrawlError("GPSJam daily response has an unexpected CSV schema")
        return [
            _response(manifest_url, manifest_body, include_records=False),
            _response(
                daily_url,
                daily_body,
                context={"snapshot_date": snapshot_date},
            ),
        ], {"snapshot_date": snapshot_date}
    if strategy == "unhcr_population_pages":
        return _fetch_unhcr_pages(fetcher, source, current_year=current_year)

    url = _request_url(source)
    return [_response(url, fetcher.fetch(source))], {}


def _xml_text(parent: ET.Element, local_name: str) -> str | None:
    for child in parent.iter():
        if child.tag.rsplit("}", 1)[-1].casefold() == local_name.casefold() and child.text:
            text = child.text.strip()
            if text:
                return text
    return None


def _feed_records(body: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise CrawlError("RSS/Atom XML parse error") from exc
    items = [
        node for node in root.iter() if node.tag.rsplit("}", 1)[-1].casefold() in {"item", "entry"}
    ]
    records = []
    for item in items:
        link = _xml_text(item, "link")
        if not link:
            for node in item:
                if node.tag.rsplit("}", 1)[-1].casefold() == "link":
                    link = node.attrib.get("href")
                    if link:
                        break
        records.append(
            {
                "record_id": _xml_text(item, "guid") or _xml_text(item, "id") or link,
                "title": _xml_text(item, "title"),
                "locator": link,
                "published_at": _xml_text(item, "pubDate")
                or _xml_text(item, "published")
                or _xml_text(item, "updated"),
                "summary": _xml_text(item, "description")
                or _xml_text(item, "summary")
                or _xml_text(item, "encoded"),
                "raw_record": ET.tostring(item, encoding="unicode"),
            }
        )
    return records


def _path_value(value: Any, path: str) -> Any:
    if not path:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            return None
    return current


def _json_records(body: bytes, record_path: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CrawlError("JSON parse error") from exc
    selected = _path_value(payload, record_path)
    if isinstance(selected, list):
        values = selected
    elif isinstance(selected, dict):
        values = [selected]
    elif isinstance(payload, list) and not record_path:
        values = payload
    elif isinstance(payload, dict) and not record_path:
        values = [payload]
    else:
        raise CrawlError(f"JSON record path {record_path!r} did not resolve to an object or list")
    return [
        {"raw_record": value}
        for value in values
        if isinstance(value, (dict, list, str, int, float, bool))
    ]


def _csv_records(body: bytes) -> list[dict[str, Any]]:
    try:
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"), newline="")))
        if not rows:
            raise CrawlError("CSV response has no header")
        first_value = rows[0][0].strip() if rows[0] else ""
        has_header = not re.fullmatch(r"\d{4}-\d{2}-\d{2}", first_value)
        if has_header:
            headers = rows.pop(0)
            return [{"raw_record": dict(zip(headers, row, strict=False))} for row in rows if row]
        width = max(len(row) for row in rows)
        headers = [f"column_{index + 1}" for index in range(width)]
        return [{"raw_record": dict(zip(headers, row, strict=False))} for row in rows if row]
    except UnicodeDecodeError as exc:
        raise CrawlError("CSV response is not UTF-8") from exc


def _normal_records(
    source: Mapping[str, Any],
    body: bytes,
    retrieved_at: str,
    *,
    source_uri: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    kind = source["kind"]
    if kind == "rss":
        rows = _feed_records(body)
    elif kind == "json":
        rows = _json_records(body, str(source.get("record_path", "")))
    elif kind == "csv":
        rows = _csv_records(body)
    else:
        try:
            html = body.decode("utf-8", errors="replace")
        except UnicodeError as exc:
            raise CrawlError("HTML response decode failed") from exc
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else None
        rows = [{"title": title, "raw_record": {"snapshot_sha256": _sha256(body)}}]

    normalized = []
    for row in rows:
        raw = row.get("raw_record")
        raw_for_fields: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {"value": raw}
        raw_for_fields.update(context or {})
        row_digest = _sha256(_canonical_json(raw_for_fields))
        configured_id_fields = source.get("record_id_fields", [])
        configured_id_values = [
            raw_for_fields.get(item) for item in configured_id_fields if isinstance(item, str)
        ]
        configured_record_id = (
            ":".join(str(value).strip() for value in configured_id_values)
            if configured_id_values
            and len(configured_id_values) == len(configured_id_fields)
            and all(value is not None and str(value).strip() for value in configured_id_values)
            else None
        )
        record_id = (
            row.get("record_id")
            or configured_record_id
            or _first_value(
                raw_for_fields,
                ("id", "guid", "url", "link", "GlobalEventID", "eventid", "icao24", "mmsi", "imo"),
            )
            or row_digest
        )
        record = {
            "source_id": source["id"],
            "source_group": source["group"],
            "provider": source["provider"],
            "record_id": str(record_id),
            "source_record_key": "urn:sha256:" + _sha256(f"{source['id']}:{record_id}".encode()),
            "record_sha256": row_digest,
            "retrieved_at": retrieved_at,
            "source_uri": source_uri or _request_url(source),
            "title": row.get("title")
            or _first_value(raw_for_fields, ("title", "name", "headline", "description")),
            "locator": row.get("locator")
            or _first_value(raw_for_fields, ("url", "link", "sourceUrl"))
            or _request_url(source),
            "published_at": row.get("published_at")
            or _first_value(
                raw_for_fields, ("published_at", "publishedAt", "date", "timestamp", "time")
            ),
            "summary": row.get("summary")
            or _first_value(raw_for_fields, ("summary", "description", "content", "text")),
            "raw_record": raw_for_fields,
        }
        normalized.append(record)
    return normalized


def _first_value(value: Mapping[str, Any], names: Sequence[str]) -> str | None:
    for name in names:
        item = value.get(name)
        if item is not None and str(item).strip():
            return str(item).strip()
    return None


def _write_immutable(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _sha256(path.read_bytes()) != _sha256(body):
            raise CrawlError(f"content hash collision at {path}")
        return
    descriptor, temp_name = tempfile.mkstemp(prefix=".raw-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError:
        if _sha256(path.read_bytes()) != _sha256(body):
            raise CrawlError(f"content hash collision at {path}") from None
    finally:
        temp_path.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _atomic_records(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _load_checkpoint(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CrawlError(f"invalid source checkpoint {path.name}: {exc}") from exc
    return value if isinstance(value, dict) else None


def _update_manifest(
    output_dir: Path,
    config: Mapping[str, Any],
    source_results: Mapping[str, Mapping[str, Any]],
    *,
    run_id: str,
    selected_source_ids: Sequence[str],
) -> dict[str, Any]:
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "upstream": config["upstream"],
        "config_sha256": _sha256(_canonical_json(config)),
        "selected_source_ids": list(selected_source_ids),
        "source_results": list(source_results.values()),
        "summary": {
            "sources_total": len(config["sources"]),
            "sources_selected": len(selected_source_ids),
            "sources_complete": sum(
                result.get("status") == "complete" for result in source_results.values()
            ),
            "sources_partial": sum(
                result.get("status") == "partial" for result in source_results.values()
            ),
            "sources_skipped": sum(
                result.get("status") == "skipped" for result in source_results.values()
            ),
            "sources_rate_limited": sum(
                result.get("status") == "rate_limited" for result in source_results.values()
            ),
            "sources_auth_required": sum(
                result.get("status") == "auth_required" for result in source_results.values()
            ),
            "sources_blocked": sum(
                result.get("status") == "blocked" for result in source_results.values()
            ),
            "sources_failed": sum(
                result.get("status") == "failed" for result in source_results.values()
            ),
            "records": sum(
                int(result.get("record_count", 0)) for result in source_results.values()
            ),
        },
        "record_file": "records.jsonl",
    }
    _atomic_json(output_dir / "manifest.json", manifest)
    return manifest


def crawl_sources(
    config: Mapping[str, Any],
    output_dir: Path,
    *,
    run_id: str,
    selected_groups: set[str] | None = None,
    selected_ids: set[str] | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists() and not resume:
        raise FileExistsError(f"run already exists at {output_dir}; pass --resume to continue")
    if resume and manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("config_sha256") != _sha256(_canonical_json(config)):
            raise CrawlError("source config changed since this crawl run; use a new run ID")
        source_results = {
            str(item["source_id"]): item
            for item in previous.get("source_results", [])
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        }
    else:
        source_results = {}

    fetcher = HttpFetcher()
    group_names = {item["id"]: item["name"] for item in config["groups"]}
    selected_sources = [
        source
        for source in config["sources"]
        if (selected_groups is None or source["group"] in selected_groups)
        and (selected_ids is None or source["id"] in selected_ids)
    ]
    if not selected_sources:
        raise CrawlError("source selection is empty")
    selected_source_ids = [source["id"] for source in selected_sources]
    if resume and manifest_path.exists():
        previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous_manifest.get("selected_source_ids") != selected_source_ids:
            raise CrawlError("source selection changed since this crawl run; use a new run ID")

    for source in selected_sources:
        source_id = source["id"]
        source_config_hash = _source_fingerprint(source)
        checkpoint_path = output_dir / "checkpoints" / f"{source_id}.json"
        record_path = output_dir / "records" / f"{source_id}.jsonl"
        previous = _load_checkpoint(checkpoint_path)
        if (
            resume
            and previous is not None
            and previous.get("source_config_sha256") == source_config_hash
            and previous.get("status") == "complete"
            and record_path.is_file()
        ):
            source_results[source_id] = previous["result"]
            continue

        base_result = {
            "source_id": source_id,
            "source_group": source["group"],
            "group_name": group_names[source["group"]],
            "provider": source["provider"],
            "access": source["access"],
            "attribution": source["attribution"],
        }
        access = str(source["access"])
        if access.startswith("excluded_"):
            result = {
                **base_result,
                "status": "skipped",
                "reason": access,
                "record_count": 0,
            }
            source_results[source_id] = result
            continue
        if not isinstance(source.get("url"), str):
            result = {
                **base_result,
                "status": "skipped",
                "reason": access,
                "record_count": 0,
            }
            source_results[source_id] = result
            continue
        if source["access"] != "public" and not _credential_envs(source):
            result = {
                **base_result,
                "status": "skipped",
                "reason": source["access"],
                "record_count": 0,
            }
            source_results[source_id] = result
            continue
        if _credential_envs(source) and len(_credential_values(source)) != len(
            _credential_envs(source)
        ):
            result = {
                **base_result,
                "status": "skipped",
                "reason": "required credential is unset",
                "credential_env": list(_credential_envs(source)),
                "record_count": 0,
            }
            source_results[source_id] = result
            continue
        supported_credential = source.get("credential_location") in {
            "header",
            "query",
        } or "{credential}" in str(source.get("url", ""))
        if source["access"] != "public" and not supported_credential:
            result = {
                **base_result,
                "status": "skipped",
                "reason": source["access"],
                "record_count": 0,
            }
            source_results[source_id] = result
            continue

        retrieved_at = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            responses, fetch_metadata = _fetch_source_responses(
                fetcher, source, current_year=datetime.now(UTC).year
            )
            raw_snapshots = []
            normalized = []
            for fetched in responses:
                body = fetched["body"]
                raw_hash = _sha256(body)
                raw_path = output_dir / "raw" / source_id / f"{raw_hash}.raw"
                _write_immutable(raw_path, body)
                raw_snapshots.append(
                    {
                        "url": fetched["url"],
                        "sha256": raw_hash,
                        "bytes": len(body),
                        "path": str(raw_path),
                    }
                )
                if fetched["include_records"]:
                    normalized.extend(
                        _normal_records(
                            source,
                            body,
                            retrieved_at,
                            source_uri=fetched["url"],
                            context=fetched["context"],
                        )
                    )
            _atomic_records(record_path, normalized)
            response_hashes = [item["sha256"] for item in raw_snapshots]
            response_hash = (
                response_hashes[0]
                if len(response_hashes) == 1
                else _sha256(_canonical_json(response_hashes))
            )
            status = "partial" if fetch_metadata.get("pagination_truncated") else "complete"
            result = {
                **base_result,
                "status": status,
                "record_count": len(normalized),
                "retrieved_at": retrieved_at,
                "response_bytes": sum(item["bytes"] for item in raw_snapshots),
                "response_sha256": response_hash,
                "raw_snapshot": raw_snapshots[-1]["path"],
                "raw_snapshots": raw_snapshots,
                "records_sha256": _sha256(record_path.read_bytes()),
                **fetch_metadata,
            }
            _atomic_json(
                checkpoint_path,
                {
                    "schema_version": 1,
                    "source_id": source_id,
                    "source_config_sha256": source_config_hash,
                    "status": status,
                    "response_sha256": response_hash,
                    "result": result,
                    "checkpointed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )
        except (
            CrawlError,
            OSError,
            ET.ParseError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            status = _failure_status(exc)
            result = {
                **base_result,
                "status": status,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "record_count": 0,
            }
        source_results[source_id] = result
        _update_manifest(
            output_dir,
            config,
            source_results,
            run_id=run_id,
            selected_source_ids=selected_source_ids,
        )
        print(f"[{source_id}] {result['status']} records={result.get('record_count', 0)}")

    records_file = output_dir / "records.jsonl"
    descriptor, temp_name = tempfile.mkstemp(prefix=".records-", dir=output_dir)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            for source in selected_sources:
                source_records = output_dir / "records" / f"{source['id']}.jsonl"
                if source_records.is_file():
                    with source_records.open("rb") as incoming:
                        shutil.copyfileobj(incoming, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        temp_path.replace(records_file)
    finally:
        temp_path.unlink(missing_ok=True)

    manifest = _update_manifest(
        output_dir,
        config,
        source_results,
        run_id=run_id,
        selected_source_ids=selected_source_ids,
    )
    manifest["record_file_sha256"] = _sha256_file(records_file)
    _atomic_json(manifest_path, manifest)
    return manifest


def _failure_status(error: Exception) -> str:
    http_status = getattr(error, "http_status", None)
    if http_status == 401:
        return "auth_required"
    if http_status in {403, 406}:
        return "blocked"
    if http_status == 429:
        return "rate_limited"
    return "failed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--group", action="append", dest="groups")
    parser.add_argument("--source", action="append", dest="sources")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("--run-id may contain only letters, digits, dash and underscore")
    try:
        config = _read_config(args.config)
        output_dir = args.output_root.expanduser() / args.run_id
        manifest = crawl_sources(
            config,
            output_dir,
            run_id=args.run_id,
            selected_groups=set(args.groups) if args.groups else None,
            selected_ids=set(args.sources) if args.sources else None,
            resume=args.resume,
        )
    except (CrawlError, FileExistsError, OSError) as exc:
        parser.error(str(exc))
    print(
        json.dumps({"manifest": str(output_dir / "manifest.json"), **manifest["summary"]}, indent=2)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
