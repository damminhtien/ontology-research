"""Bounded OSINT connectors and mappers for the Vietnam-South China Sea AOI.

Connectors preserve source responses. Mappers expose only fields supported by
the source schemas; they do not resolve identity or create world assertions.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import time
import urllib.parse
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from foundry.provenance import content_sha256
from foundry.source_adapters import (
    ConnectorPage,
    RawRecord,
    SourceAdapterError,
    SourceContract,
    source_record_hash,
)

OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
GDELT_LASTUPDATE_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GDELT_EVENTS_URL_PATTERN = re.compile(
    r"https?://[^\s]+/gdeltv2/(?P<timestamp>\d{14})\.export\.CSV\.zip\Z"
)
AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
GDELT_FIELD_COUNT = 61


@dataclass(frozen=True)
class GeoBounds:
    """Rectangular area of interest; it is not a political boundary."""

    name: str
    south: float
    west: float
    north: float
    east: float

    def __post_init__(self) -> None:
        """Reject empty names and invalid geographic extents."""
        if not self.name.strip():
            raise ValueError("region name must be non-empty")
        if not (-90 <= self.south < self.north <= 90):
            raise ValueError("latitude bounds must satisfy -90 <= south < north <= 90")
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError("longitude bounds must satisfy -180 <= west < east <= 180")

    def contains(self, latitude: object, longitude: object) -> bool:
        """Return whether finite numeric coordinates lie inside the AOI."""
        if isinstance(latitude, bool) or isinstance(longitude, bool):
            return False
        try:
            lat = float(latitude)  # type: ignore[arg-type]
            lon = float(longitude)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(lat)
            and math.isfinite(lon)
            and self.south <= lat <= self.north
            and self.west <= lon <= self.east
        )

    def opensky_query(self) -> str:
        """Return the geographic OpenSky query parameters."""
        return urllib.parse.urlencode(
            {
                "lamin": self.south,
                "lomin": self.west,
                "lamax": self.north,
                "lomax": self.east,
            }
        )

    def ais_bounding_box(self) -> list[list[float]]:
        """Return AISStream's south-west and north-east lat/lon corners."""
        return [[self.south, self.west], [self.north, self.east]]


VIETNAM_SCS = GeoBounds(
    name="vietnam-scs",
    south=0.0,
    west=98.0,
    north=25.0,
    east=122.0,
)


@dataclass(frozen=True)
class PositionRecord:
    """A source-reported platform position with absolute source time."""

    source_id: str
    source_record_id: str
    position_record_id: str
    source_uri: str
    locator: str
    entity_name: str
    external_namespace: str
    external_id: str
    observed_at: str
    valid_until: str
    latitude: float
    longitude: float
    identity_content_hash: str
    measurements: tuple[dict[str, object], ...]

    @property
    def location_iri(self) -> str:
        """Stable CRS84 point identifier at the source's coordinate precision."""
        latitude = 0.0 if self.latitude == 0 else self.latitude
        longitude = 0.0 if self.longitude == 0 else self.longitude
        return f"urn:osint:crs84:{latitude:.6f}:{longitude:.6f}"


@dataclass(frozen=True)
class AisReportRecord:
    """AIS report archived as a source document without inferred UTC date."""

    source_id: str
    record_id: str
    source_uri: str
    locator: str
    title: str
    text: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class GdeltReportRecord:
    """GDELT-coded event row retained as a source report, not a confirmed fact."""

    source_id: str
    record_id: str
    source_uri: str
    locator: str
    title: str
    text: str
    event_day: str
    event_code: str
    event_root_code: str
    quad_class: int | None
    goldstein_scale: float | None
    actor1: str
    actor2: str
    action_location: str
    source_url: str
    latitude: float
    longitude: float


class OpenSkyConnector:
    """Fetch one current OpenSky state-vector snapshot for a bounded AOI."""

    def __init__(
        self,
        *,
        contract: SourceContract,
        http,
        region: GeoBounds = VIETNAM_SCS,
        api_url: str = OPENSKY_STATES_URL,
    ) -> None:
        """Configure the bounded snapshot endpoint and region fingerprint."""
        self.contract = contract
        self._http = http
        self._region = region
        self._url = f"{api_url}?{region.opensky_query()}"
        self.request_fingerprint = content_sha256(
            json.dumps(
                {"url": self._url, "region": region.name},
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        """Fetch and preserve one exact state-vector snapshot."""
        if cursor is not None:
            raise SourceAdapterError("OpenSky snapshots do not accept a cursor")
        body, payload = self._http.get_json(self._url)
        snapshot_time = payload.get("time")
        if isinstance(snapshot_time, bool) or not isinstance(snapshot_time, int):
            raise SourceAdapterError("OpenSky response has no integer snapshot time")
        record = RawRecord(
            source_id=self.contract.id,
            record_id=f"states:{snapshot_time}",
            source_uri=self._url,
            schema_version=self.contract.record_schema,
            body=body,
            retrieved_at=datetime.now(UTC).isoformat(),
        )
        return ConnectorPage((record,), None, True)


class OpenSkyMapper:
    """Map valid in-region vectors to position claims with source timestamps."""

    mapping_version = "1"

    def __init__(
        self,
        *,
        region: GeoBounds = VIETNAM_SCS,
        max_position_age_seconds: int = 3600,
        max_records: int = 500,
    ) -> None:
        """Set the AOI and maximum accepted source-position age."""
        if max_position_age_seconds < 0:
            raise ValueError("max_position_age_seconds must be non-negative")
        if max_records < 1:
            raise ValueError("max_records must be positive")
        self.region = region
        self.max_position_age_seconds = max_position_age_seconds
        self.max_records = max_records
        self.mapping_version = (
            f"1|region={region.name}|max_position_age={max_position_age_seconds}"
            f"|max_records={max_records}"
        )

    def map(self, record: RawRecord) -> tuple[PositionRecord, ...]:
        """Filter state vectors and retain only complete, recent positions."""
        try:
            payload = json.loads(record.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceAdapterError("OpenSky snapshot is not valid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("time"), int):
            raise SourceAdapterError("OpenSky snapshot must contain an integer time")
        states = payload.get("states") or []
        if not isinstance(states, list):
            raise SourceAdapterError("OpenSky states must be a list or null")

        snapshot_time = int(payload["time"])
        results: list[PositionRecord] = []
        for state in states:
            parsed = _parse_opensky_state(state)
            if parsed is None:
                continue
            icao24, callsign, position_time, latitude, longitude, rest = parsed
            if not self.region.contains(latitude, longitude):
                continue
            age = snapshot_time - position_time
            if age < 0 or age > self.max_position_age_seconds:
                continue
            observed_at = datetime.fromtimestamp(position_time, UTC).isoformat()
            snapshot_id = record.record_id.removeprefix("states:")
            record_id = f"{snapshot_id}:{icao24}"
            locator = f"{record.source_uri}#time={snapshot_id}&icao24={icao24}"
            origin_country = rest[0]
            identity_payload = {
                "icao24": icao24,
                "origin_country": origin_country,
            }
            measurements: list[dict[str, object]] = [
                _measurement("latitude", latitude, "degree"),
                _measurement("longitude", longitude, "degree"),
            ]
            baro_altitude = rest[5]
            on_ground = rest[6]
            velocity = rest[7]
            heading = rest[8]
            vertical_rate = rest[9]
            geo_altitude = rest[11]
            for name, value, unit in (
                ("velocity", velocity, "m/s"),
                ("heading", heading, "degree"),
                ("vertical_rate", vertical_rate, "m/s"),
                ("barometric_altitude", baro_altitude, "m"),
                ("geometric_altitude", geo_altitude, "m"),
            ):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    measurements.append(_measurement(name, float(value), unit))
            if isinstance(on_ground, bool):
                measurements.append(_measurement("on_ground", on_ground, None))
            if callsign:
                measurements.append(_measurement("callsign", callsign, None))
            if isinstance(origin_country, str) and origin_country:
                measurements.append(_measurement("origin_country", origin_country, None))
            results.append(
                PositionRecord(
                    source_id=record.source_id,
                    source_record_id=icao24,
                    position_record_id=record_id,
                    source_uri=record.source_uri,
                    locator=locator,
                    entity_name=f"Aircraft{icao24}",
                    external_namespace="opensky-icao24",
                    external_id=icao24,
                    observed_at=observed_at,
                    valid_until=datetime.fromtimestamp(position_time + 1, UTC).isoformat(),
                    latitude=latitude,
                    longitude=longitude,
                    identity_content_hash=source_record_hash(identity_payload),
                    measurements=tuple(measurements),
                )
            )
            if len(results) >= self.max_records:
                break
        return tuple(results)


class GdeltConnector:
    """Fetch one GDELT 2.0 event export named by its latest-update manifest."""

    def __init__(
        self,
        *,
        contract: SourceContract,
        http,
        lastupdate_url: str = GDELT_LASTUPDATE_URL,
    ) -> None:
        """Configure discovery of the newest official event export."""
        self.contract = contract
        self._http = http
        self._lastupdate_url = lastupdate_url
        self.request_fingerprint = content_sha256(
            json.dumps(
                {"lastupdate_url": lastupdate_url, "format": "events-2.0"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        """Discover and retrieve the newest complete events export."""
        if cursor is not None:
            raise SourceAdapterError("GDELT latest-export connector does not accept a cursor")
        try:
            manifest = self._http.get(self._lastupdate_url, accept="text/plain").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceAdapterError("GDELT lastupdate manifest is not UTF-8") from exc
        export = _parse_gdelt_manifest(manifest)
        if export is None:
            raise SourceAdapterError("GDELT manifest has no current events export")
        source_uri, timestamp, expected_md5 = export
        body = self._http.get(source_uri, accept="application/zip")
        if hashlib.md5(body).hexdigest().casefold() != expected_md5.casefold():
            raise SourceAdapterError("GDELT events export does not match manifest MD5")
        record = RawRecord(
            source_id=self.contract.id,
            record_id=f"events:{timestamp}",
            source_uri=source_uri,
            schema_version=self.contract.record_schema,
            body=body,
            retrieved_at=datetime.now(UTC).isoformat(),
        )
        return ConnectorPage((record,), None, True)


class GdeltEventMapper:
    """Map regional rows to provenance documents; GDELT codes remain reports."""

    mapping_version = "1"

    def __init__(self, *, region: GeoBounds = VIETNAM_SCS, max_records: int = 500) -> None:
        """Set the regional row limit and mapper version fingerprint."""
        if max_records < 1:
            raise ValueError("max_records must be positive")
        self.region = region
        self.max_records = max_records
        self.mapping_version = f"1|region={region.name}|max_records={max_records}"

    def map(self, record: RawRecord) -> tuple[GdeltReportRecord, ...]:
        """Read the zipped tab-separated export without extracting it to disk."""
        reports: list[GdeltReportRecord] = []
        try:
            with zipfile.ZipFile(io.BytesIO(record.body)) as archive:
                members = [name for name in archive.namelist() if not name.endswith("/")]
                if len(members) != 1:
                    raise SourceAdapterError("GDELT export must contain exactly one data file")
                with archive.open(members[0]) as binary_stream:
                    text_stream = io.TextIOWrapper(binary_stream, encoding="utf-8", newline="")
                    reader = csv.reader(text_stream, delimiter="\t")
                    for row in reader:
                        report = self._map_row(record, row)
                        if report is not None:
                            reports.append(report)
                            if len(reports) >= self.max_records:
                                break
        except (zipfile.BadZipFile, UnicodeDecodeError, csv.Error) as exc:
            raise SourceAdapterError(f"invalid GDELT event archive: {exc}") from exc
        return tuple(reports)

    def _map_row(self, record: RawRecord, row: list[str]) -> GdeltReportRecord | None:
        if len(row) != GDELT_FIELD_COUNT:
            raise SourceAdapterError(
                f"GDELT event row has {len(row)} columns; expected {GDELT_FIELD_COUNT}"
            )
        if not row[0].isdigit() or not re.fullmatch(r"\d{8}", row[1]):
            raise SourceAdapterError("GDELT event row has an invalid ID or day")
        latitude = parse_finite_float(row[56])
        longitude = parse_finite_float(row[57])
        if latitude is None or longitude is None or not self.region.contains(latitude, longitude):
            return None
        try:
            day = datetime.strptime(row[1], "%Y%m%d").date().isoformat()
            quad_class = int(row[29]) if row[29] else None
            goldstein = float(row[30]) if row[30] else None
        except ValueError as exc:
            raise SourceAdapterError(f"invalid GDELT event values for {row[0]}") from exc
        if goldstein is not None and not math.isfinite(goldstein):
            raise SourceAdapterError(f"non-finite GDELT Goldstein score for {row[0]}")
        event = {
            "global_event_id": row[0],
            "event_day": day,
            "actor1": row[6],
            "actor2": row[16],
            "event_code": row[26],
            "event_base_code": row[27],
            "event_root_code": row[28],
            "quad_class": quad_class,
            "goldstein_scale": goldstein,
            "action_location": row[52],
            "action_latitude": latitude,
            "action_longitude": longitude,
            "source_url": row[60],
        }
        event_json = json.dumps(event, ensure_ascii=False, sort_keys=True)
        locator = f"{record.source_uri}#GlobalEventID={row[0]}"
        return GdeltReportRecord(
            source_id=record.source_id,
            record_id=row[0],
            source_uri=record.source_uri,
            locator=locator,
            title=f"GDELT event {row[0]} · CAMEO {row[26] or 'unknown'}",
            text=event_json,
            event_day=day,
            event_code=row[26],
            event_root_code=row[28],
            quad_class=quad_class,
            goldstein_scale=goldstein,
            actor1=row[6],
            actor2=row[16],
            action_location=row[52],
            source_url=row[60],
            latitude=latitude,
            longitude=longitude,
        )


class AisStreamConnector:
    """Capture a bounded AISStream window with server-side credentials only."""

    def __init__(
        self,
        *,
        contract: SourceContract,
        api_key: str,
        duration_seconds: int = 15,
        max_messages: int = 500,
        region: GeoBounds = VIETNAM_SCS,
        connect: Callable[..., Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Set credentials, area and strict time/message bounds."""
        if not api_key.strip():
            raise ValueError("AISSTREAM_API_KEY is required")
        if not 1 <= duration_seconds <= 300:
            raise ValueError("AIS duration must be in [1, 300] seconds")
        if max_messages < 1:
            raise ValueError("AIS max_messages must be positive")
        self.contract = contract
        self._api_key = api_key
        self._duration = duration_seconds
        self._max_messages = max_messages
        self._region = region
        self._connect = connect
        self._monotonic = monotonic
        self._sleep = sleep
        self.request_fingerprint = content_sha256(
            json.dumps(
                {
                    "endpoint": AISSTREAM_URL,
                    "region": region.name,
                    "duration_seconds": duration_seconds,
                    "max_messages": max_messages,
                    "message_types": ["PositionReport"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        """Collect one time-bounded snapshot and preserve each JSON frame as a line."""
        if cursor is not None:
            raise SourceAdapterError("AISStream snapshots do not accept a cursor")
        connect = self._connect
        if connect is None:
            try:
                from websockets.sync.client import connect as websockets_connect
            except ImportError as exc:
                raise SourceAdapterError(
                    "AISStream requires requirements-osint.txt (websockets==15.0.1)"
                ) from exc
            connect = websockets_connect

        started = self._monotonic()
        frames = self._collect(connect, started)
        body = b"\n".join(frames)
        record = RawRecord(
            source_id=self.contract.id,
            record_id=f"stream:{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}",
            source_uri=AISSTREAM_URL,
            schema_version=self.contract.record_schema,
            body=body,
            retrieved_at=datetime.now(UTC).isoformat(),
        )
        return ConnectorPage((record,), None, True)

    def _collect(self, connect: Callable[..., Any], started: float) -> list[bytes]:
        frames: list[bytes] = []
        reconnects = 0
        while len(frames) < self._max_messages:
            remaining = self._duration - (self._monotonic() - started)
            if remaining <= 0:
                break
            try:
                with connect(
                    AISSTREAM_URL,
                    compression="deflate",
                    open_timeout=min(10, remaining),
                ) as websocket:
                    subscription = {
                        "APIKey": self._api_key,
                        "BoundingBoxes": [self._region.ais_bounding_box()],
                        "FilterMessageTypes": ["PositionReport"],
                    }
                    websocket.send(json.dumps(subscription, separators=(",", ":")))
                    reconnects = 0
                    while len(frames) < self._max_messages:
                        remaining = self._duration - (self._monotonic() - started)
                        if remaining <= 0:
                            break
                        try:
                            frame = websocket.recv(timeout=min(5.0, remaining))
                        except TimeoutError:
                            continue
                        if isinstance(frame, str):
                            frame = frame.encode("utf-8")
                        if not isinstance(frame, bytes):
                            raise SourceAdapterError("AISStream returned a non-text/binary frame")
                        try:
                            decoded = json.loads(frame)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise SourceAdapterError("AISStream returned invalid JSON") from exc
                        if not isinstance(decoded, dict):
                            raise SourceAdapterError("AISStream frame must contain a JSON object")
                        if decoded.get("MessageType") != "PositionReport":
                            if decoded.get("MessageType") == "SubscriptionConfirmation":
                                continue
                            if decoded.get("MessageType") == "Error":
                                raise SourceAdapterError("AISStream rejected the subscription")
                            continue
                        frames.append(frame)
                        reconnects = 0
                    if (
                        len(frames) >= self._max_messages
                        or self._monotonic() - started >= self._duration
                    ):
                        break
            except SourceAdapterError:
                raise
            except Exception as exc:
                if exc.__class__.__module__.startswith("websockets") or isinstance(exc, OSError):
                    reconnects += 1
                    if reconnects > 3:
                        raise SourceAdapterError("AISStream reconnect limit exceeded") from exc
                    remaining = self._duration - (self._monotonic() - started)
                    self._sleep(min(float(2 ** (reconnects - 1)), max(0.0, remaining)))
                    continue
                raise SourceAdapterError(
                    f"AISStream connection failed: {type(exc).__name__}"
                ) from exc
        return frames


class AisStreamMapper:
    """Keep validated in-AOI position reports as source documents."""

    mapping_version = "1"

    def __init__(self, *, region: GeoBounds = VIETNAM_SCS, max_records: int = 500) -> None:
        """Set the region and report cap used in the checkpoint fingerprint."""
        if max_records < 1:
            raise ValueError("max_records must be positive")
        self.region = region
        self.max_records = max_records
        self.mapping_version = f"1|region={region.name}|max_records={max_records}"

    def map(self, record: RawRecord) -> tuple[AisReportRecord, ...]:
        """Map valid position frames while preserving their source-only time."""
        reports: list[AisReportRecord] = []
        for line_number, raw_line in enumerate(record.body.splitlines(), start=1):
            try:
                payload = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SourceAdapterError(
                    f"invalid AISStream JSON frame at line {line_number}"
                ) from exc
            if not isinstance(payload, dict) or payload.get("MessageType") != "PositionReport":
                continue
            metadata = payload.get("MetaData")
            message = payload.get("Message")
            details = message.get("PositionReport") if isinstance(message, dict) else None
            if not isinstance(metadata, dict) or not isinstance(details, dict):
                continue
            if details.get("Valid") is not True:
                continue
            latitude = parse_finite_float(metadata.get("Latitude", details.get("Latitude")))
            longitude = parse_finite_float(metadata.get("Longitude", details.get("Longitude")))
            mmsi = metadata.get("MMSI", details.get("UserID"))
            if (
                latitude is None
                or longitude is None
                or not self.region.contains(latitude, longitude)
            ):
                continue
            if isinstance(mmsi, bool) or not str(mmsi).isdigit() or len(str(mmsi)) != 9:
                continue
            digest = content_sha256(raw_line.decode("utf-8"))
            record_id = f"message:{mmsi}:{digest}"
            reports.append(
                AisReportRecord(
                    source_id=record.source_id,
                    record_id=record_id,
                    source_uri=record.source_uri,
                    locator=f"{record.source_uri}#{record.record_id}:{line_number}",
                    title=f"AISStream position report MMSI {mmsi}",
                    text=raw_line.decode("utf-8"),
                    latitude=latitude,
                    longitude=longitude,
                )
            )
            if len(reports) >= self.max_records:
                break
        return tuple(reports)


def _parse_opensky_state(
    state: object,
) -> tuple[str, str, int, float, float, tuple[object, ...]] | None:
    """Validate the positional OpenSky state-vector schema."""
    if not isinstance(state, list) or len(state) < 17:
        return None
    icao24 = state[0]
    if not isinstance(icao24, str) or not re.fullmatch(r"[0-9a-fA-F]{6}", icao24):
        return None
    position_time = state[3]
    latitude = parse_finite_float(state[6])
    longitude = parse_finite_float(state[5])
    if (
        isinstance(position_time, bool)
        or not isinstance(position_time, int)
        or latitude is None
        or longitude is None
    ):
        return None
    callsign = state[1].strip() if isinstance(state[1], str) else ""
    return icao24.casefold(), callsign, position_time, latitude, longitude, tuple(state[2:])


def _parse_gdelt_manifest(manifest: str) -> tuple[str, str, str] | None:
    """Read GDELT's size, MD5 and URL rows without depending on column order."""
    for line in manifest.splitlines():
        tokens = line.split()
        matched_url = next(
            (
                (token, GDELT_EVENTS_URL_PATTERN.fullmatch(token))
                for token in tokens
                if GDELT_EVENTS_URL_PATTERN.fullmatch(token)
            ),
            None,
        )
        if matched_url is None:
            continue
        raw_url, url_match = matched_url
        digest = next(
            (token for token in tokens if re.fullmatch(r"[0-9a-fA-F]{32}", token)),
            None,
        )
        if digest is None:
            continue
        parsed_url = urllib.parse.urlparse(raw_url)
        if parsed_url.hostname != "data.gdeltproject.org":
            continue
        timestamp = url_match.group("timestamp")
        secure_url = urllib.parse.urlunparse(parsed_url._replace(scheme="https"))
        return secure_url, timestamp, digest.casefold()
    return None


def _measurement(name: str, value: object, unit: str | None) -> dict[str, object]:
    result: dict[str, object] = {"name": name, "value": value}
    if unit is not None:
        result["unit"] = unit
    return result


def parse_finite_float(value: object) -> float | None:
    """Return a finite numeric value while rejecting booleans and nulls."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
