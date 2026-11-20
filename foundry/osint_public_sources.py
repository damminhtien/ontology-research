"""Regional public-source connectors that preserve evidence without asserting facts."""

from __future__ import annotations

import csv
import io
import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from foundry.osint_sources import VIETNAM_SCS, GeoBounds, parse_finite_float
from foundry.provenance import content_sha256
from foundry.source_adapters import (
    ConnectorPage,
    RawRecord,
    SourceAdapterError,
    SourceContract,
)

OSM_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USGS_WEEKLY_GEOJSON_URL = (
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_week.geojson"
)
FIRMS_AREA_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
GFW_EVENTS_URL = "https://gateway.api.globalfishingwatch.org/v3/events"
GDACS_EVENTS_URL = "https://www.gdacs.org/gdacsapi/api/Events/geteventlist/eventsbyarea"
FIRMS_PRODUCT = "VIIRS_NOAA20_NRT"
GFW_DATASET = "public-global-fishing-events:latest"


@dataclass(frozen=True)
class RegionalReportRecord:
    """One source report suitable for document registration with provenance."""

    source_id: str
    record_id: str
    locator: str
    title: str
    text: str


class OverpassConnector:
    """Fetch a bounded set of named port and harbor features from OpenStreetMap."""

    def __init__(
        self,
        *,
        contract: SourceContract,
        http: Any,
        region: GeoBounds = VIETNAM_SCS,
    ) -> None:
        """Configure the Overpass query and stable area fingerprint."""
        self.contract = contract
        self._http = http
        self._region = region
        self._tiles = _split_bounds(region, rows=2, columns=2)
        self._queries = tuple(_overpass_query(tile) for tile in self._tiles)
        self.request_fingerprint = content_sha256(
            json.dumps(
                {"endpoint": OSM_OVERPASS_URL, "queries": self._queries},
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        """Fetch one spatial tile so failed tiles can be resumed."""
        try:
            tile_index = int(cursor) if cursor is not None else 0
        except ValueError as exc:
            raise SourceAdapterError("Overpass cursor must be a tile index") from exc
        if not 0 <= tile_index < len(self._tiles):
            raise SourceAdapterError("Overpass cursor is outside the configured tile set")
        tile = self._tiles[tile_index]
        query = self._queries[tile_index]
        form = urllib.parse.urlencode({"data": query}).encode("ascii")
        body = self._http.post(
            OSM_OVERPASS_URL,
            form,
            accept="application/json",
            content_type="application/x-www-form-urlencoded; charset=utf-8",
        )
        payload = _load_json_object(body, "OpenStreetMap Overpass")
        if not isinstance(payload.get("elements"), list):
            raise SourceAdapterError("Overpass response has no elements array")
        record = RawRecord(
            source_id=self.contract.id,
            record_id=f"tile:{tile_index}:{content_sha256(body)[:16]}",
            source_uri=f"{OSM_OVERPASS_URL}#region={tile.name}",
            schema_version=self.contract.record_schema,
            body=body,
            retrieved_at=datetime.now(UTC).isoformat(),
        )
        complete = tile_index == len(self._tiles) - 1
        next_cursor = None if complete else str(tile_index + 1)
        return ConnectorPage((record,), next_cursor, complete)


class OverpassMapper:
    """Map port/harbor tags to source documents with local AOI revalidation."""

    mapping_version = "1"

    def __init__(
        self,
        *,
        region: GeoBounds = VIETNAM_SCS,
        max_records: int = 500,
        already_mapped: int = 0,
    ) -> None:
        """Set the local area filter and remaining run-wide output cap."""
        self.region = region
        self.max_records = _positive_limit(max_records)
        if already_mapped < 0:
            raise ValueError("already_mapped must be non-negative")
        self._remaining = max(0, max_records - already_mapped)
        self.mapping_version = f"1|region={region.name}|max_records={max_records}"

    def map(self, record: RawRecord) -> tuple[RegionalReportRecord, ...]:
        """Return valid OSM features with a location and stable source ID."""
        payload = _load_json_object(record.body, "OpenStreetMap Overpass")
        elements = payload.get("elements")
        if not isinstance(elements, list):
            raise SourceAdapterError("Overpass response has no elements array")
        reports: list[RegionalReportRecord] = []
        if self._remaining == 0:
            return ()
        for element in elements:
            if not isinstance(element, dict):
                continue
            element_type = element.get("type")
            element_id = element.get("id")
            tags = element.get("tags")
            point = element if "lat" in element else element.get("center")
            if (
                element_type not in {"node", "way", "relation"}
                or isinstance(element_id, bool)
                or not isinstance(element_id, int)
                or not isinstance(tags, dict)
                or not isinstance(point, dict)
            ):
                continue
            latitude = parse_finite_float(point.get("lat"))
            longitude = parse_finite_float(point.get("lon"))
            if (
                latitude is None
                or longitude is None
                or not self.region.contains(latitude, longitude)
            ):
                continue
            record_id = f"{element_type}:{element_id}"
            name = tags.get("name")
            title = name.strip() if isinstance(name, str) and name.strip() else f"OSM {record_id}"
            details = {
                "osm_type": element_type,
                "osm_id": element_id,
                "latitude": latitude,
                "longitude": longitude,
                "tags": tags,
            }
            reports.append(
                RegionalReportRecord(
                    source_id=record.source_id,
                    record_id=record_id,
                    locator=f"https://www.openstreetmap.org/{element_type}/{element_id}",
                    title=f"OpenStreetMap feature: {title}",
                    text=json.dumps(details, ensure_ascii=False, sort_keys=True),
                )
            )
            self._remaining -= 1
            if self._remaining == 0:
                break
        return tuple(reports)


class UsgsWeeklyConnector:
    """Fetch the current USGS weekly earthquake GeoJSON feed."""

    def __init__(
        self,
        *,
        contract: SourceContract,
        http: Any,
        region: GeoBounds = VIETNAM_SCS,
    ) -> None:
        """Configure the global weekly feed and region fingerprint."""
        self.contract = contract
        self._http = http
        self._region = region
        self.request_fingerprint = content_sha256(
            json.dumps(
                {"url": USGS_WEEKLY_GEOJSON_URL, "region": region.name},
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        """Fetch exact weekly feed bytes for deterministic local filtering."""
        if cursor is not None:
            raise SourceAdapterError("USGS weekly snapshot does not accept a cursor")
        body, payload = self._http.get_json(USGS_WEEKLY_GEOJSON_URL)
        if payload.get("type") != "FeatureCollection" or not isinstance(
            payload.get("features"), list
        ):
            raise SourceAdapterError("USGS weekly response is not a GeoJSON FeatureCollection")
        metadata = payload.get("metadata")
        generated = metadata.get("generated") if isinstance(metadata, dict) else None
        snapshot = str(generated) if isinstance(generated, int) else content_sha256(body)[:16]
        record = RawRecord(
            source_id=self.contract.id,
            record_id=f"weekly:{snapshot}",
            source_uri=USGS_WEEKLY_GEOJSON_URL,
            schema_version=self.contract.record_schema,
            body=body,
            retrieved_at=datetime.now(UTC).isoformat(),
        )
        return ConnectorPage((record,), None, True)


class UsgsEarthquakeMapper:
    """Map in-area USGS earthquake reports without inferring impact or damage."""

    mapping_version = "1"

    def __init__(self, *, region: GeoBounds = VIETNAM_SCS, max_records: int = 500) -> None:
        """Set local area validation and maximum report count."""
        self.region = region
        self.max_records = _positive_limit(max_records)
        self.mapping_version = f"1|region={region.name}|max_records={max_records}"

    def map(self, record: RawRecord) -> tuple[RegionalReportRecord, ...]:
        """Retain source event ID, origin time, location and magnitude fields."""
        payload = _load_json_object(record.body, "USGS earthquake feed")
        features = payload.get("features")
        if not isinstance(features, list):
            raise SourceAdapterError("USGS response has no features array")
        reports: list[RegionalReportRecord] = []
        for feature in features:
            if not isinstance(feature, dict):
                continue
            event_id = feature.get("id")
            properties = feature.get("properties")
            geometry = feature.get("geometry")
            coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
            if (
                not isinstance(event_id, str)
                or not event_id
                or not isinstance(properties, dict)
                or not isinstance(coordinates, list)
                or len(coordinates) < 2
            ):
                continue
            longitude = parse_finite_float(coordinates[0])
            latitude = parse_finite_float(coordinates[1])
            if (
                longitude is None
                or latitude is None
                or not self.region.contains(latitude, longitude)
            ):
                continue
            details = {
                "event_id": event_id,
                "time_ms": properties.get("time"),
                "updated_ms": properties.get("updated"),
                "magnitude": properties.get("mag"),
                "place": properties.get("place"),
                "latitude": latitude,
                "longitude": longitude,
                "depth_km": coordinates[2] if len(coordinates) > 2 else None,
                "url": properties.get("url"),
                "status": properties.get("status"),
            }
            place = properties.get("place") or event_id
            reports.append(
                RegionalReportRecord(
                    source_id=record.source_id,
                    record_id=event_id,
                    locator=str(properties.get("url") or f"{record.source_uri}#{event_id}"),
                    title=f"USGS earthquake report: {place}",
                    text=json.dumps(details, ensure_ascii=False, sort_keys=True),
                )
            )
            if len(reports) >= self.max_records:
                break
        return tuple(reports)


class FirmsAreaConnector:
    """Fetch a one-day NASA FIRMS VIIRS area CSV using an environment key."""

    def __init__(
        self,
        *,
        contract: SourceContract,
        http: Any,
        map_key: str,
        snapshot_date: date,
        region: GeoBounds = VIETNAM_SCS,
    ) -> None:
        """Configure bounded product/date request without retaining its secret URL."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", map_key.strip()):
            raise ValueError("NASA FIRMS MAP_KEY has an invalid format")
        self.contract = contract
        self._http = http
        self._map_key = map_key.strip()
        self._date = snapshot_date
        self._region = region
        coordinates = _firms_bbox(region)
        self._source_uri = (
            f"{FIRMS_AREA_URL}/[MAP_KEY]/{FIRMS_PRODUCT}/{coordinates}/1/"
            f"{snapshot_date.isoformat()}"
        )
        self._request_url = (
            f"{FIRMS_AREA_URL}/{urllib.parse.quote(self._map_key, safe='')}/"
            f"{FIRMS_PRODUCT}/{coordinates}/1/{snapshot_date.isoformat()}"
        )
        self.request_fingerprint = content_sha256(
            json.dumps(
                {"source_uri": self._source_uri, "region": region.name},
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        """Fetch exact CSV bytes while redacting the path key on failures."""
        if cursor is not None:
            raise SourceAdapterError("NASA FIRMS snapshots do not accept a cursor")
        try:
            body = self._http.get(self._request_url, accept="text/csv")
        except SourceAdapterError:
            raise SourceAdapterError("NASA FIRMS request failed; check MAP_KEY and quota") from None
        if not body:
            raise SourceAdapterError("NASA FIRMS returned an empty CSV response")
        record = RawRecord(
            source_id=self.contract.id,
            record_id=f"{FIRMS_PRODUCT}:{self._date.isoformat()}:{self._region.name}",
            source_uri=self._source_uri,
            schema_version=self.contract.record_schema,
            body=body,
            retrieved_at=datetime.now(UTC).isoformat(),
        )
        return ConnectorPage((record,), None, True)


class FirmsHotspotMapper:
    """Map valid FIRMS detections inside the AOI to evidence documents."""

    mapping_version = "1"

    def __init__(self, *, region: GeoBounds = VIETNAM_SCS, max_records: int = 500) -> None:
        """Set local coordinate validation and report cap."""
        self.region = region
        self.max_records = _positive_limit(max_records)
        self.mapping_version = f"1|region={region.name}|max_records={max_records}"

    def map(self, record: RawRecord) -> tuple[RegionalReportRecord, ...]:
        """Validate the documented CSV columns and preserve each source row."""
        try:
            reader = csv.DictReader(io.StringIO(record.body.decode("utf-8-sig"), newline=""))
            required = {"latitude", "longitude", "acq_date", "acq_time", "satellite"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise SourceAdapterError("NASA FIRMS CSV is missing required columns")
            reports: list[RegionalReportRecord] = []
            for row_number, row in enumerate(reader, start=2):
                latitude = parse_finite_float(row.get("latitude"))
                longitude = parse_finite_float(row.get("longitude"))
                if (
                    latitude is None
                    or longitude is None
                    or not self.region.contains(latitude, longitude)
                ):
                    continue
                normalized = {key: value for key, value in row.items() if key and value is not None}
                identity = content_sha256(
                    json.dumps(normalized, sort_keys=True, separators=(",", ":"))
                )
                details = {
                    **normalized,
                    "latitude": latitude,
                    "longitude": longitude,
                    "source_coordinates": "latitude,longitude",
                }
                reports.append(
                    RegionalReportRecord(
                        source_id=record.source_id,
                        record_id=f"hotspot:{identity}",
                        locator=f"{record.source_uri}#row={row_number}",
                        title=(
                            f"NASA FIRMS {normalized.get('satellite', 'VIIRS')} detection "
                            f"{normalized.get('acq_date', '')} {normalized.get('acq_time', '')}"
                        ).strip(),
                        text=json.dumps(details, ensure_ascii=False, sort_keys=True),
                    )
                )
                if len(reports) >= self.max_records:
                    break
            return tuple(reports)
        except (UnicodeDecodeError, csv.Error) as exc:
            raise SourceAdapterError(f"invalid NASA FIRMS CSV: {exc}") from exc


class GlobalFishingWatchConnector:
    """Fetch a bounded recent fishing-events page with a bearer token."""

    def __init__(
        self,
        *,
        contract: SourceContract,
        http: Any,
        api_token: str,
        start_date: date,
        end_date: date,
        max_records: int = 500,
        region: GeoBounds = VIETNAM_SCS,
    ) -> None:
        """Configure one date-bounded area request without storing credentials."""
        if not api_token.strip():
            raise ValueError("Global Fishing Watch token is required")
        if start_date >= end_date:
            raise ValueError("GFW start date must be before exclusive end date")
        self.contract = contract
        self._http = http
        self._token = api_token.strip()
        self._start_date = start_date
        self._end_date = end_date
        self._max_records = _positive_limit(max_records)
        self._region = region
        self._body = json.dumps(
            {
                "datasets": [GFW_DATASET],
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "geometry": _region_polygon(region),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self._request_uri = f"{GFW_EVENTS_URL}?limit={self._max_records}&offset=0"
        self.request_fingerprint = content_sha256(
            json.dumps(
                {
                    "url": self._request_uri,
                    "body": json.loads(self._body),
                    "region": region.name,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        """Fetch and validate an exact GFW response using header-only auth."""
        if cursor is not None:
            raise SourceAdapterError("GFW bounded snapshot does not accept a cursor")
        body = self._http.post(
            self._request_uri,
            self._body,
            accept="application/json",
            content_type="application/json",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        payload = _load_json_object(body, "Global Fishing Watch")
        if not isinstance(payload.get("entries"), list):
            raise SourceAdapterError("GFW response has no entries array")
        record_id = f"events:{self._start_date.isoformat()}:{self._end_date.isoformat()}"
        record = RawRecord(
            source_id=self.contract.id,
            record_id=record_id,
            source_uri=self._request_uri,
            schema_version=self.contract.record_schema,
            body=body,
            retrieved_at=datetime.now(UTC).isoformat(),
        )
        return ConnectorPage((record,), None, True)


class GlobalFishingWatchMapper:
    """Map in-AOI GFW event rows to source documents, never identity claims."""

    mapping_version = "1"

    def __init__(self, *, region: GeoBounds = VIETNAM_SCS, max_records: int = 500) -> None:
        """Set local area validation and report cap."""
        self.region = region
        self.max_records = _positive_limit(max_records)
        self.mapping_version = f"1|region={region.name}|max_records={max_records}"

    def map(self, record: RawRecord) -> tuple[RegionalReportRecord, ...]:
        """Retain source event IDs, type, times and vessel metadata."""
        payload = _load_json_object(record.body, "Global Fishing Watch")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise SourceAdapterError("GFW response has no entries array")
        reports: list[RegionalReportRecord] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            event_id = entry.get("id")
            position = entry.get("position")
            if not isinstance(event_id, str) or not event_id or not isinstance(position, dict):
                continue
            latitude = parse_finite_float(position.get("lat"))
            longitude = parse_finite_float(position.get("lon"))
            if (
                latitude is None
                or longitude is None
                or not self.region.contains(latitude, longitude)
            ):
                continue
            details = {
                "event_id": event_id,
                "type": entry.get("type"),
                "start": entry.get("start"),
                "end": entry.get("end"),
                "position": {"latitude": latitude, "longitude": longitude},
                "vessel": entry.get("vessel"),
                "source_dataset": GFW_DATASET,
            }
            event_type = str(entry.get("type") or "event")
            reports.append(
                RegionalReportRecord(
                    source_id=record.source_id,
                    record_id=event_id,
                    locator=f"{record.source_uri}#{event_id}",
                    title=f"Global Fishing Watch {event_type} event {event_id}",
                    text=json.dumps(details, ensure_ascii=False, sort_keys=True),
                )
            )
            if len(reports) >= self.max_records:
                break
        return tuple(reports)


class GdacsConnector:
    """Fetch GDACS GeoJSON alerts selected by a regional polygon and day window."""

    def __init__(
        self,
        *,
        contract: SourceContract,
        http: Any,
        region: GeoBounds = VIETNAM_SCS,
        days: int = 4,
    ) -> None:
        """Configure a spatial query and stable area/window fingerprint."""
        if not 1 <= days <= 30:
            raise ValueError("GDACS days must be in [1, 30]")
        self.contract = contract
        self._http = http
        self._region = region
        self._days = days
        geometry = _region_wkt(region)
        query = urllib.parse.urlencode({"geometryArea": geometry, "days": days})
        self._url = f"{GDACS_EVENTS_URL}?{query}"
        self.request_fingerprint = content_sha256(
            json.dumps({"url": self._url}, sort_keys=True, separators=(",", ":"))
        )

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        """Fetch and preserve the bounded regional GeoJSON feature collection."""
        if cursor is not None:
            raise SourceAdapterError("GDACS area query does not accept a cursor")
        body, payload = self._http.get_json(self._url)
        if payload.get("type") != "FeatureCollection" or not isinstance(
            payload.get("features"), list
        ):
            raise SourceAdapterError("GDACS response is not a GeoJSON FeatureCollection")
        record = RawRecord(
            source_id=self.contract.id,
            record_id=f"events:{content_sha256(body)[:16]}",
            source_uri=self._url,
            schema_version=self.contract.record_schema,
            body=body,
            retrieved_at=datetime.now(UTC).isoformat(),
        )
        return ConnectorPage((record,), None, True)


class GdacsEventMapper:
    """Map in-AOI GDACS alerts to source documents without treating alerts as facts."""

    mapping_version = "1"

    def __init__(self, *, region: GeoBounds = VIETNAM_SCS, max_records: int = 500) -> None:
        """Set local area validation and report cap."""
        self.region = region
        self.max_records = _positive_limit(max_records)
        self.mapping_version = f"1|region={region.name}|max_records={max_records}"

    def map(self, record: RawRecord) -> tuple[RegionalReportRecord, ...]:
        """Retain provider event/episode IDs, alert attributes and point geometry."""
        payload = _load_json_object(record.body, "GDACS")
        features = payload.get("features")
        if not isinstance(features, list):
            raise SourceAdapterError("GDACS response has no features array")
        reports: list[RegionalReportRecord] = []
        for feature in features:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties")
            geometry = feature.get("geometry")
            coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
            if (
                not isinstance(properties, dict)
                or not isinstance(coordinates, list)
                or len(coordinates) < 2
            ):
                continue
            longitude = parse_finite_float(coordinates[0])
            latitude = parse_finite_float(coordinates[1])
            event_type = properties.get("eventtype")
            event_id = properties.get("eventid")
            episode_id = properties.get("episodeid")
            if (
                longitude is None
                or latitude is None
                or not self.region.contains(latitude, longitude)
                or not isinstance(event_type, str)
                or isinstance(event_id, bool)
                or not isinstance(event_id, int | str)
            ):
                continue
            record_id = f"{event_type}:{event_id}:{episode_id}"
            links = properties.get("url")
            locator = (
                str(links.get("report"))
                if isinstance(links, dict) and isinstance(links.get("report"), str)
                else f"{record.source_uri}#{record_id}"
            )
            details = {
                "event_type": event_type,
                "event_id": event_id,
                "episode_id": episode_id,
                "name": properties.get("name"),
                "alert_level": properties.get("alertlevel"),
                "alert_score": properties.get("alertscore"),
                "from_date": properties.get("fromdate"),
                "to_date": properties.get("todate"),
                "modified_at": properties.get("datemodified"),
                "country": properties.get("country"),
                "affected_countries": properties.get("affectedcountries"),
                "source": properties.get("source"),
                "latitude": latitude,
                "longitude": longitude,
            }
            title = properties.get("name") or f"GDACS {event_type} event {event_id}"
            reports.append(
                RegionalReportRecord(
                    source_id=record.source_id,
                    record_id=record_id,
                    locator=locator,
                    title=str(title),
                    text=json.dumps(details, ensure_ascii=False, sort_keys=True),
                )
            )
            if len(reports) >= self.max_records:
                break
        return tuple(reports)


def _split_bounds(region: GeoBounds, *, rows: int, columns: int) -> tuple[GeoBounds, ...]:
    """Split a broad regional AOI into deterministic Overpass-sized boxes."""
    if rows < 1 or columns < 1:
        raise ValueError("tile rows and columns must be positive")
    latitude_step = (region.north - region.south) / rows
    longitude_step = (region.east - region.west) / columns
    return tuple(
        GeoBounds(
            name=f"{region.name}-r{row}-c{column}",
            south=region.south + row * latitude_step,
            west=region.west + column * longitude_step,
            north=region.north if row == rows - 1 else region.south + (row + 1) * latitude_step,
            east=region.east
            if column == columns - 1
            else region.west + (column + 1) * longitude_step,
        )
        for row in range(rows)
        for column in range(columns)
    )


def _overpass_query(region: GeoBounds) -> str:
    """Build a small tag-focused query and constrain server memory/time."""
    box = f"{region.south},{region.west},{region.north},{region.east}"
    return (
        "[out:json][timeout:25][maxsize:52428800];"
        '(nwr["amenity"="port"](' + box + ");"
        'nwr["seamark:type"="harbour"](' + box + ");"
        'nwr["harbour"="yes"](' + box + "););"
        "out center tags;"
    )


def _firms_bbox(region: GeoBounds) -> str:
    """Return FIRMS' documented west,south,east,north coordinate order."""
    return f"{region.west:g},{region.south:g},{region.east:g},{region.north:g}"


def _region_polygon(region: GeoBounds) -> dict[str, object]:
    """Convert the rectangular AOI to a closed GeoJSON Polygon ring."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [region.west, region.south],
                [region.east, region.south],
                [region.east, region.north],
                [region.west, region.north],
                [region.west, region.south],
            ]
        ],
    }


def _region_wkt(region: GeoBounds) -> str:
    """Convert the rectangular AOI to a closed WKT polygon ring."""
    return (
        "POLYGON (("
        f"{region.west:g} {region.south:g}, "
        f"{region.east:g} {region.south:g}, "
        f"{region.east:g} {region.north:g}, "
        f"{region.west:g} {region.north:g}, "
        f"{region.west:g} {region.south:g}"
        "))"
    )


def _load_json_object(body: bytes, source_name: str) -> dict[str, object]:
    """Parse a JSON object with source-specific safe errors."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceAdapterError(f"{source_name} response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SourceAdapterError(f"{source_name} response must be a JSON object")
    return payload


def _positive_limit(value: int) -> int:
    """Validate and return a positive record limit."""
    if isinstance(value, bool) or value < 1:
        raise ValueError("max_records must be positive")
    return value
