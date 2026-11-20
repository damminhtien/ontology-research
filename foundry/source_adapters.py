"""Shared connector, immutable archive and checkpoint contracts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol

from foundry.provenance import content_sha256


class SourceAdapterError(RuntimeError):
    """Raised when an external connector response or source contract is invalid."""


@dataclass(frozen=True)
class SourceContract:
    """Source policy and mapping declaration loaded from the source registry."""

    id: str
    identifier: str
    record_schema: str
    license: str
    storage_policy: str
    redistribution_policy: str
    freshness_policy: str
    identifier_rules: str
    mapping_rules: str
    data_quality_checks: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SourceContract:
        """Validate and construct one source contract."""
        required = (
            "id",
            "identifier",
            "record_schema",
            "license",
            "storage_policy",
            "redistribution_policy",
            "freshness_policy",
            "identifier_rules",
            "mapping_rules",
        )
        missing = [key for key in required if not isinstance(value.get(key), str) or not value[key]]
        checks = value.get("data_quality_checks")
        if (
            missing
            or not isinstance(checks, list)
            or not checks
            or any(not isinstance(check, str) or not check.strip() for check in checks)
        ):
            raise SourceAdapterError(
                f"invalid source contract; missing={missing}, quality_checks={checks!r}"
            )
        return cls(
            id=str(value["id"]),
            identifier=str(value["identifier"]),
            record_schema=str(value["record_schema"]),
            license=str(value["license"]),
            storage_policy=str(value["storage_policy"]),
            redistribution_policy=str(value["redistribution_policy"]),
            freshness_policy=str(value["freshness_policy"]),
            identifier_rules=str(value["identifier_rules"]),
            mapping_rules=str(value["mapping_rules"]),
            data_quality_checks=tuple(checks),
        )


def load_source_contracts(path: Path) -> dict[str, SourceContract]:
    """Load the declared source registry, rejecting duplicate or incomplete IDs."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceAdapterError(f"cannot read source registry {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise SourceAdapterError(f"unsupported source registry schema at {path}")
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise SourceAdapterError("source registry must contain a sources list")
    contracts = [SourceContract.from_dict(item) for item in raw_sources if isinstance(item, dict)]
    if len(contracts) != len(raw_sources):
        raise SourceAdapterError("every source registry entry must be an object")
    result = {contract.id: contract for contract in contracts}
    if len(result) != len(contracts):
        raise SourceAdapterError("source registry contains duplicate IDs")
    return result


@dataclass(frozen=True)
class RawRecord:
    """One exact response body ready for archival before domain mapping."""

    source_id: str
    record_id: str
    source_uri: str
    schema_version: str
    body: bytes
    retrieved_at: str

    @property
    def content_hash(self) -> str:
        """SHA-256 of the exact response bytes."""
        return hashlib.sha256(self.body).hexdigest()


@dataclass(frozen=True)
class ConnectorPage:
    """Raw page and continuation state returned by a source connector."""

    records: tuple[RawRecord, ...]
    next_cursor: str | None
    complete: bool


class SourceConnector(Protocol):
    """Network adapter. It fetches source bytes and does no domain mapping."""

    contract: SourceContract
    request_fingerprint: str

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        """Fetch one bounded page after ``cursor``."""
        ...


class DomainMapper[T](Protocol):
    """Pure mapper from archived raw bytes to canonical domain records."""

    mapping_version: str

    def map(self, record: RawRecord) -> Sequence[T]:
        """Return zero or more canonical records without network or storage IO."""
        ...


@dataclass(frozen=True)
class PageRun:
    """Summary of one successfully archived, mapped and checkpointed page."""

    source_id: str
    raw_records: int
    mapped_records: int
    snapshot_hashes: tuple[str, ...]
    complete: bool


def run_connector_page[T](
    connector: SourceConnector,
    mapper: DomainMapper[T],
    *,
    archive: RawArchive,
    checkpoints: CheckpointStore,
    handle: Callable[[RawRecord, Sequence[T], ArchivedRecord], None],
) -> PageRun | None:
    """Archive and map a page; advance its checkpoint only after handling succeeds.

    ``handle`` must return after every mapped command is either durably committed
    or durably queued for review. If it raises, the previous checkpoint remains
    authoritative and the page is safe to fetch again.
    """
    previous = checkpoints.load(connector.contract.id)
    connector_fingerprint = connector.request_fingerprint
    if len(connector_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in connector_fingerprint
    ):
        raise SourceAdapterError("connector request_fingerprint must be lowercase SHA-256")
    mapping_version = getattr(mapper, "mapping_version", "1")
    if not isinstance(mapping_version, str) or not mapping_version.strip():
        raise SourceAdapterError("domain mapper must declare a non-empty mapping_version")
    request_fingerprint = content_sha256(
        json.dumps(
            {
                "connector": connector_fingerprint,
                "record_schema": connector.contract.record_schema,
                "mapping_version": mapping_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if previous is not None and previous.get("request_fingerprint") != request_fingerprint:
        raise SourceAdapterError(
            f"checkpoint configuration does not match {connector.contract.id}; use a new run ID"
        )
    if previous is not None and previous.get("complete") is True:
        return None
    cursor_value = previous.get("cursor") if previous else None
    cursor = cursor_value if isinstance(cursor_value, str) else None
    page = connector.fetch_page(cursor)
    if not page.records:
        raise SourceAdapterError(f"{connector.contract.id} returned an empty page")
    if page.complete and page.next_cursor is not None:
        raise SourceAdapterError("completed connector page must not have a next cursor")
    if not page.complete and (page.next_cursor is None or page.next_cursor == cursor):
        raise SourceAdapterError("non-complete connector page must advance its cursor")
    snapshots: list[str] = []
    mapped_count = int(previous.get("mapped_records", 0)) if previous else 0
    raw_count = int(previous.get("raw_records", 0)) if previous else 0
    for record in page.records:
        archived = archive.store(record, connector.contract)
        mapped = tuple(mapper.map(record))
        handle(record, mapped, archived)
        snapshots.append(archived.content_hash)
        mapped_count += len(mapped)
        raw_count += 1
    checkpoints.save(
        connector.contract.id,
        cursor=page.next_cursor,
        complete=page.complete,
        last_snapshot_hash=snapshots[-1],
        request_fingerprint=request_fingerprint,
        mapped_records=mapped_count,
        raw_records=raw_count,
    )
    return PageRun(
        source_id=connector.contract.id,
        raw_records=raw_count,
        mapped_records=mapped_count,
        snapshot_hashes=tuple(snapshots),
        complete=page.complete,
    )


@dataclass(frozen=True)
class ArchivedRecord:
    """Content-addressed blob path and digest for one raw response."""

    path: Path
    content_hash: str


class RawArchive:
    """Append-only, content-addressed raw response storage."""

    def __init__(self, root: Path) -> None:
        """Create an archive rooted outside source control when used by the CLI."""
        self.root = root

    def store(self, record: RawRecord, contract: SourceContract) -> ArchivedRecord:
        """Store exact response bytes and append retrieval metadata to a JSONL manifest."""
        if record.source_id != contract.id:
            raise SourceAdapterError("raw record source does not match its declared contract")
        if record.schema_version != contract.record_schema:
            raise SourceAdapterError(
                "raw record schema version does not match its declared contract"
            )
        digest = record.content_hash
        source_dir = self.root / record.source_id
        source_dir.mkdir(parents=True, exist_ok=True)
        blob_path = source_dir / f"{digest}.raw"
        self._write_immutable(blob_path, record.body)
        manifest = {
            "source_id": record.source_id,
            "record_id": record.record_id,
            "source_uri": record.source_uri,
            "schema_version": record.schema_version,
            "retrieved_at": record.retrieved_at,
            "content_hash": digest,
            "blob": blob_path.name,
            "license": contract.license,
        }
        manifest_path = source_dir / "manifest.jsonl"
        with manifest_path.open("ab") as stream:
            stream.write(
                (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
        return ArchivedRecord(blob_path, digest)

    def read_latest(self, source_id: str, record_id: str, contract: SourceContract) -> RawRecord:
        """Read the latest archived response for a stable source record key."""
        if source_id != contract.id:
            raise SourceAdapterError("raw record source does not match its declared contract")
        manifest_path = self.root / source_id / "manifest.jsonl"
        try:
            lines = manifest_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SourceAdapterError(f"cannot read raw manifest {manifest_path}: {exc}") from exc
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SourceAdapterError(f"corrupt raw manifest {manifest_path}: {exc}") from exc
            if not isinstance(entry, dict) or entry.get("record_id") != record_id:
                continue
            if entry.get("source_id") != source_id:
                raise SourceAdapterError(f"raw manifest source mismatch in {manifest_path}")
            blob_name = entry.get("blob")
            digest = entry.get("content_hash")
            if not isinstance(blob_name, str) or not isinstance(digest, str):
                raise SourceAdapterError(f"malformed raw manifest entry in {manifest_path}")
            if entry.get("schema_version") != contract.record_schema:
                raise SourceAdapterError("archived raw schema version does not match its contract")
            body = (manifest_path.parent / blob_name).read_bytes()
            if hashlib.sha256(body).hexdigest() != digest:
                raise SourceAdapterError(
                    f"archived raw hash mismatch: {manifest_path.parent / blob_name}"
                )
            return RawRecord(
                source_id=source_id,
                record_id=record_id,
                source_uri=str(entry["source_uri"]),
                schema_version=str(entry["schema_version"]),
                body=body,
                retrieved_at=str(entry["retrieved_at"]),
            )
        raise SourceAdapterError(f"no archived response for {source_id}/{record_id}")

    @staticmethod
    def _write_immutable(path: Path, body: bytes) -> None:
        """Publish a blob without ever replacing an existing content address."""
        if path.exists():
            if path.read_bytes() != body:
                raise SourceAdapterError(f"content-address collision at {path}")
            return
        descriptor, temporary_name = tempfile.mkstemp(prefix=".raw-", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                if path.read_bytes() != body:
                    raise SourceAdapterError(f"content-address collision at {path}") from None
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary_path.unlink(missing_ok=True)


class CheckpointStore:
    """Atomic per-source page checkpoint storage."""

    SCHEMA_VERSION = 2

    def __init__(self, root: Path) -> None:
        """Set the checkpoint directory."""
        self.root = root

    def load(self, source_id: str) -> dict[str, object] | None:
        """Read one source checkpoint, validating its source identity."""
        path = self.root / f"{source_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceAdapterError(f"cannot read checkpoint {path}: {exc}") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != self.SCHEMA_VERSION
            or value.get("source_id") != source_id
        ):
            raise SourceAdapterError(f"invalid checkpoint for {source_id}")
        return value

    def save(
        self,
        source_id: str,
        *,
        cursor: str | None,
        complete: bool,
        last_snapshot_hash: str,
        request_fingerprint: str,
        mapped_records: int = 0,
        raw_records: int = 0,
    ) -> None:
        """Atomically advance the checkpoint after all page records commit."""
        for name, digest in (
            ("last_snapshot_hash", last_snapshot_hash),
            ("request_fingerprint", request_fingerprint),
        ):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if mapped_records < 0 or raw_records < 0:
            raise ValueError("checkpoint record counts must be non-negative")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{source_id}.json"
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "source_id": source_id,
            "cursor": cursor,
            "complete": complete,
            "last_snapshot_hash": last_snapshot_hash,
            "request_fingerprint": request_fingerprint,
            "mapped_records": mapped_records,
            "raw_records": raw_records,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{source_id}-", dir=self.root)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(path)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary_path.unlink(missing_ok=True)


class JsonHttpClient:
    """Sequential JSON client with per-host rate limiting and bounded retry."""

    RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        user_agent: str,
        minimum_interval: float = 1.0,
        retries: int = 3,
        backoff: float = 1.0,
        opener: Callable[..., object] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure sequential request pacing and retry behavior."""
        if minimum_interval < 0 or retries < 0 or backoff < 0:
            raise ValueError("rate interval, retry count and backoff must be non-negative")
        if not user_agent.strip():
            raise ValueError("a descriptive User-Agent is required")
        self._user_agent = user_agent
        self._minimum_interval = minimum_interval
        self._retries = retries
        self._backoff = backoff
        self._opener = opener
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: dict[str, float] = {}

    def get(self, url: str, *, accept: str, headers: Mapping[str, str] | None = None) -> bytes:
        """Fetch exact bytes and retry only transient HTTP or transport errors."""
        return self._request(url, method="GET", body=None, accept=accept, headers=headers)

    def post(
        self,
        url: str,
        body: bytes,
        *,
        accept: str,
        content_type: str,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """Post exact bytes with the same pacing and retry contract as GET."""
        request_headers = dict(headers or {})
        request_headers["Content-Type"] = content_type
        return self._request(url, method="POST", body=body, accept=accept, headers=request_headers)

    def _request(
        self,
        url: str,
        *,
        method: str,
        body: bytes | None,
        accept: str,
        headers: Mapping[str, str] | None,
    ) -> bytes:
        """Run one paced request with bounded retries."""
        host = urllib.parse.urlparse(url).netloc
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            last_request = self._last_request_at.get(host)
            if last_request is not None:
                remaining = self._minimum_interval - (self._clock() - last_request)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request_at[host] = self._clock()
            request_headers = {"User-Agent": self._user_agent, "Accept": accept}
            request_headers.update(headers or {})
            request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
            try:
                response = self._opener(request, timeout=30)
                with response as opened:
                    return opened.read()
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in self.RETRYABLE_STATUS or attempt == self._retries:
                    raise SourceAdapterError(f"HTTP {exc.code} fetching {url}") from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = self._retry_delay(attempt, retry_after)
                self._sleep(delay)
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt == self._retries:
                    break
                self._sleep(self._backoff * (2**attempt))
        raise SourceAdapterError(
            f"request failed after {self._retries + 1} tries: {url}"
        ) from last_error

    def get_json(
        self, url: str, *, accept: str = "application/json"
    ) -> tuple[bytes, dict[str, object]]:
        """Fetch and parse a JSON response without discarding its exact bytes."""
        body = self.get(url, accept=accept)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceAdapterError(f"invalid JSON response from {url}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SourceAdapterError(f"JSON response from {url} must be an object")
        return body, payload

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        """Use numeric Retry-After when supplied, otherwise exponential backoff."""
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    retry_time = parsedate_to_datetime(retry_after)
                    if retry_time.tzinfo is None:
                        retry_time = retry_time.replace(tzinfo=UTC)
                    return max(0.0, (retry_time - datetime.now(UTC)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return self._backoff * (2**attempt)


def source_record_hash(value: Mapping[str, object]) -> str:
    """Hash one parsed source record using deterministic canonical JSON."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return content_sha256(encoded)
