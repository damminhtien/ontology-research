"""Offline contracts for Vietnam-SCS OSINT adapters and source semantics."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import urllib.parse
import zipfile
from datetime import date
from pathlib import Path

import pytest
from conftest import CORE_ONTOLOGY, SHAPES_FILE

from foundry.events import EventLog
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline
from foundry.namespaces import CORE_LOCATED_AT
from foundry.osint_public_sources import (
    FirmsAreaConnector,
    FirmsHotspotMapper,
    GdacsConnector,
    GdacsEventMapper,
    GlobalFishingWatchConnector,
    GlobalFishingWatchMapper,
    OverpassConnector,
    OverpassMapper,
    UsgsEarthquakeMapper,
    UsgsWeeklyConnector,
)
from foundry.osint_sources import (
    VIETNAM_SCS,
    AdsbPositionMapper,
    AdsbSnapshotConnector,
    AisStreamConnector,
    AisStreamMapper,
    GdeltConnector,
    GdeltEventMapper,
    GeoBounds,
    OpenSkyConnector,
    OpenSkyMapper,
)
from foundry.source_adapters import RawRecord, SourceAdapterError, SourceContract

REGISTRY = Path(__file__).resolve().parents[1] / "config" / "source_registry.json"


def _contract(source_id: str) -> SourceContract:
    from foundry.source_adapters import load_source_contracts

    return load_source_contracts(REGISTRY)[source_id]


def test_crawl_cli_emits_compact_source_progress(capsys):
    from ingest_osint_region import _print_source_progress

    _print_source_progress(
        "opensky",
        {"status": "complete", "mapped_records": 12, "documents": 3, "error": "hidden"},
    )

    assert json.loads(capsys.readouterr().out) == {
        "documents": 3,
        "mapped_records": 12,
        "source": "opensky",
        "status": "complete",
    }


def _raw_record(
    source_id: str, body: bytes, *, uri: str = "https://example.org/snapshot"
) -> RawRecord:
    return RawRecord(
        source_id=source_id,
        record_id="snapshot:1700000030",
        source_uri=uri,
        schema_version="test-v1",
        body=body,
        retrieved_at="2026-11-16T00:00:00Z",
    )


def _opensky_state(
    icao24: str,
    *,
    longitude: float = 110.0,
    latitude: float = 16.0,
    position_time: int | None = 1_700_000_000,
) -> list[object]:
    return [
        icao24,
        "VN123 ",
        "Vietnam",
        position_time,
        1_700_000_020,
        longitude,
        latitude,
        1000.0,
        False,
        120.0,
        90.0,
        1.0,
        None,
        950.0,
        "1234",
        False,
        0,
    ]


def _adsb_aircraft(
    icao24: str = "abc123",
    *,
    longitude: float = 110.0,
    latitude: float = 16.0,
    seen_pos: float | None = 1.5,
) -> dict[str, object]:
    row: dict[str, object] = {
        "hex": icao24,
        "flight": "VN123 ",
        "lat": latitude,
        "lon": longitude,
        "alt_baro": 12000,
        "alt_geom": 12200,
        "gs": 250.5,
        "track": 91.0,
        "baro_rate": -128,
        "r": "VN-A123",
        "t": "A321",
        "category": "A3",
        "squawk": "1234",
    }
    if seen_pos is not None:
        row["seen_pos"] = seen_pos
    return row


def _gdelt_row(
    event_id: str = "1234567890",
    *,
    latitude: str = "16.0",
    longitude: str = "110.0",
) -> list[str]:
    row = [""] * 61
    row[0] = event_id
    row[1] = "20261115"
    row[6] = "ACTOR ONE"
    row[16] = "ACTOR TWO"
    row[26] = "190"
    row[27] = "19"
    row[28] = "19"
    row[29] = "4"
    row[30] = "-7.0"
    row[52] = "South China Sea"
    row[56] = latitude
    row[57] = longitude
    row[60] = "https://news.example/article"
    return row


def _gdelt_zip(*rows: list[str]) -> bytes:
    text = io.StringIO()
    writer = csv.writer(text, delimiter="\t", lineterminator="\n")
    writer.writerows(rows)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("events.csv", text.getvalue())
    return output.getvalue()


def test_vietnam_scs_area_is_a_rectangular_filter_not_a_boundary():
    assert VIETNAM_SCS.contains(16, 110)
    assert VIETNAM_SCS.contains(25, 122)
    assert not VIETNAM_SCS.contains(26, 110)
    assert not VIETNAM_SCS.contains(float("nan"), 110)
    assert not VIETNAM_SCS.contains(True, 110)
    with pytest.raises(ValueError, match="latitude bounds"):
        GeoBounds("bad", 90, 0, 91, 1)


def test_opensky_mapper_keeps_recent_positions_and_source_time():
    body = json.dumps(
        {
            "time": 1_700_000_030,
            "states": [
                _opensky_state("abc123"),
                _opensky_state("def456", position_time=None),
                _opensky_state("789abc", latitude=27.0),
                _opensky_state("bad789", position_time=1_699_996_000),
            ],
        }
    ).encode()
    positions = OpenSkyMapper().map(_raw_record("opensky", body))

    assert len(positions) == 1
    position = positions[0]
    assert position.source_record_id == "abc123"
    assert position.external_namespace == "opensky-icao24"
    assert position.entity_name == "Aircraftabc123"
    assert position.observed_at == "2023-11-14T22:13:20+00:00"
    assert position.valid_until == "2023-11-14T22:13:21+00:00"
    assert position.location_iri == "urn:osint:crs84:16.000000:110.000000"
    assert {item["name"] for item in position.measurements} >= {
        "latitude",
        "longitude",
        "velocity",
        "callsign",
        "origin_country",
    }


def test_opensky_connector_requests_only_the_configured_area():
    body = b'{"time":1700000030,"states":[]}'

    class FakeHttp:
        url = ""

        def get_json(self, url: str):
            self.url = url
            return body, {"time": 1_700_000_030, "states": []}

    http = FakeHttp()
    connector = OpenSkyConnector(contract=_contract("opensky"), http=http)
    page = connector.fetch_page(None)

    assert "lamin=0.0" in http.url
    assert "lomin=98.0" in http.url
    assert "lamax=25.0" in http.url
    assert "lomax=122.0" in http.url
    assert page.records[0].body == body
    assert page.records[0].record_id == "states:1700000030"


@pytest.mark.parametrize(
    ("source_id", "expected_url"),
    [
        ("adsb-lol", "https://api.adsb.lol/v2/lat/12.5/lon/110/dist/250"),
        (
            "adsb-fi",
            "https://opendata.adsb.fi/api/v3/lat/12.5/lon/110/dist/250",
        ),
    ],
)
def test_adsb_connector_uses_bounded_provider_endpoint(source_id, expected_url):
    body = b'{"ac":[],"now":1700000030000}'

    class FakeHttp:
        url = ""

        def get_json(self, url: str):
            self.url = url
            return body, {"ac": [], "now": 1_700_000_030_000}

    http = FakeHttp()
    connector = AdsbSnapshotConnector(contract=_contract(source_id), http=http)
    page = connector.fetch_page(None)

    assert http.url == expected_url
    assert page.complete and page.next_cursor is None
    assert page.records[0].body == body
    assert page.records[0].record_id == "snapshot:1700000030000"


def test_adsb_mapper_requires_recent_source_timed_positions_and_preserves_units():
    body = json.dumps(
        {
            "now": 1_700_000_030_000,
            "ac": [
                _adsb_aircraft(),
                _adsb_aircraft("def456", seen_pos=3601),
                _adsb_aircraft("789abc", latitude=27.0),
                _adsb_aircraft("bad789", seen_pos=None),
                _adsb_aircraft("bad_id"),
            ],
        }
    ).encode()
    positions = AdsbPositionMapper().map(_raw_record("adsb-lol", body))

    assert len(positions) == 1
    position = positions[0]
    assert position.external_namespace == "adsb-lol-icao24"
    assert position.external_id == "abc123"
    assert position.entity_name == "AircraftAdsbLolabc123"
    assert position.observed_at == "2023-11-14T22:13:48.500000+00:00"
    assert position.valid_until == "2023-11-14T22:13:49.500000+00:00"
    measurements = {item["name"]: item for item in position.measurements}
    assert measurements["barometric_altitude"]["unit"] == "ft"
    assert measurements["ground_speed"]["unit"] == "knot"
    assert measurements["vertical_rate"]["unit"] == "ft/min"
    assert measurements["registration"]["value"] == "VN-A123"


def test_adsb_mapper_rejects_unversioned_snapshot_time():
    body = json.dumps({"now": 1_700_000_030.0, "ac": [_adsb_aircraft()]}).encode()

    with pytest.raises(SourceAdapterError, match="integer Unix-millisecond"):
        AdsbPositionMapper().map(_raw_record("adsb-fi", body))


def test_adsb_providers_do_not_auto_merge_the_same_icao24(tmp_path):
    body = json.dumps({"now": 1_700_000_030_000, "ac": [_adsb_aircraft()]}).encode()
    log = EventLog(tmp_path / "events.jsonl")
    pipeline = IngestionPipeline(
        identity=IdentityService(), log=log, ontology_path=CORE_ONTOLOGY, shapes_path=SHAPES_FILE
    )

    entities = []
    for source_id in ("adsb-lol", "adsb-fi"):
        position = AdsbPositionMapper().map(_raw_record(source_id, body))[0]
        entity = pipeline.ingest_entity(
            name=position.entity_name,
            entity_type="Platform",
            source_id=position.source_id,
            record_id=position.source_record_id,
            external_source=position.external_namespace,
            external_id=position.external_id,
            content_hash=position.identity_content_hash,
        )
        assert entity.accepted and entity.is_new
        entities.append(entity.canonical_id)

    assert len(set(entities)) == 2
    assert [event.event_type for event in log.read_all()] == ["EntityCreated", "EntityCreated"]


def test_opensky_positions_ingest_as_idempotent_source_assertions(tmp_path):
    body = json.dumps({"time": 1_700_000_030, "states": [_opensky_state("abc123")]}).encode()
    position = OpenSkyMapper().map(_raw_record("opensky", body))[0]
    log = EventLog(tmp_path / "events.jsonl")
    pipeline = IngestionPipeline(
        identity=IdentityService(), log=log, ontology_path=CORE_ONTOLOGY, shapes_path=SHAPES_FILE
    )

    for _ in range(2):
        entity = pipeline.ingest_entity(
            name=position.entity_name,
            entity_type="Platform",
            source_id=position.source_id,
            record_id=position.source_record_id,
            external_source=position.external_namespace,
            external_id=position.external_id,
            content_hash=position.identity_content_hash,
        )
        assert entity.accepted
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
            idempotency_key=f"osint-position:{position.source_id}:{position.position_record_id}",
        )
        assert assertion.accepted

    events = log.read_all()
    assert [event.event_type for event in events] == ["EntityCreated", "AssertionMade"]
    assert events[-1].payload["valid_from"] == position.observed_at
    assert events[-1].payload["source_ids"] == ["urn:source:opensky"]


def test_distinct_opensky_records_do_not_collide_on_generated_names(tmp_path):
    body = json.dumps(
        {
            "time": 1_700_000_030,
            "states": [_opensky_state("abc123"), _opensky_state("def456")],
        }
    ).encode()
    positions = OpenSkyMapper().map(_raw_record("opensky", body))
    log = EventLog(tmp_path / "events.jsonl")
    pipeline = IngestionPipeline(
        identity=IdentityService(), log=log, ontology_path=CORE_ONTOLOGY, shapes_path=SHAPES_FILE
    )

    results = [
        pipeline.ingest_entity(
            name=position.entity_name,
            entity_type="Platform",
            source_id=position.source_id,
            record_id=position.source_record_id,
            external_source=position.external_namespace,
            external_id=position.external_id,
            content_hash=position.identity_content_hash,
        )
        for position in positions
    ]

    assert len(results) == 2
    assert all(result.accepted and result.is_new for result in results)
    assert len({result.canonical_id for result in results}) == 2
    assert [event.event_type for event in log.read_all()] == ["EntityCreated", "EntityCreated"]


def test_opensky_mapper_applies_record_limit_after_regional_validation():
    body = json.dumps(
        {
            "time": 1_700_000_030,
            "states": [_opensky_state("abc123"), _opensky_state("def456")],
        }
    ).encode()

    assert len(OpenSkyMapper(max_records=1).map(_raw_record("opensky", body))) == 1


def test_gdelt_connector_checks_manifest_hash_and_maps_regional_rows():
    body = _gdelt_zip(_gdelt_row(), _gdelt_row("1234567891", latitude="30"))
    digest = hashlib.md5(body).hexdigest()
    source_url = "https://data.gdeltproject.org/gdeltv2/20261115120000.export.CSV.zip"

    class FakeHttp:
        def get(self, url: str, *, accept: str) -> bytes:
            if url.endswith("lastupdate.txt"):
                legacy_url = source_url.replace("https://", "http://")
                return f"79131 {digest} {legacy_url}\n".encode()
            assert url == source_url
            return body

    connector = GdeltConnector(contract=_contract("gdelt-events"), http=FakeHttp())
    page = connector.fetch_page(None)
    reports = GdeltEventMapper(max_records=10).map(page.records[0])

    assert page.complete
    assert len(reports) == 1
    report = reports[0]
    assert report.event_day == "2026-11-15"
    assert report.event_code == "190"
    assert report.event_root_code == "19"
    assert report.quad_class == 4
    assert report.goldstein_scale == -7.0
    assert report.source_url == "https://news.example/article"
    assert report.latitude == 16.0 and report.longitude == 110.0
    assert '"source_url": "https://news.example/article"' in report.text


def test_gdelt_report_is_registered_as_a_document_not_a_fact(tmp_path):
    from ingest_osint_region import _register_gdelt_report

    raw = _raw_record("gdelt-events", _gdelt_zip(_gdelt_row()))
    report = GdeltEventMapper().map(raw)[0]
    log = EventLog(tmp_path / "events.jsonl")
    pipeline = IngestionPipeline(
        identity=IdentityService(), log=log, ontology_path=CORE_ONTOLOGY, shapes_path=SHAPES_FILE
    )

    _register_gdelt_report(pipeline, report)

    events = log.read_all()
    assert len(events) == 1
    assert events[0].event_type == "DocumentRegistered"
    assert events[0].payload["content_ref"] == report.locator
    assert events[0].payload["source_record_id"] == report.record_id


def test_gdelt_rejects_bad_field_count_and_manifest_checksum():
    malformed = _gdelt_zip(["bad"])
    with pytest.raises(SourceAdapterError, match="columns; expected 61"):
        GdeltEventMapper().map(_raw_record("gdelt-events", malformed))

    class BadHashHttp:
        def get(self, url: str, *, accept: str) -> bytes:
            if url.endswith("lastupdate.txt"):
                return (
                    "79131 "
                    + "0" * 32
                    + " http://data.gdeltproject.org/gdeltv2/20261115120000.export.CSV.zip"
                ).encode()
            return _gdelt_zip(_gdelt_row())

    connector = GdeltConnector(contract=_contract("gdelt-events"), http=BadHashHttp())
    with pytest.raises(SourceAdapterError, match="MD5"):
        connector.fetch_page(None)


def test_aisstream_archives_reports_without_inventing_absolute_valid_time():
    message = {
        "MessageType": "PositionReport",
        "MetaData": {
            "MMSI": 574001234,
            "ShipName": "TEST VESSEL",
            "Latitude": 12.0,
            "Longitude": 111.0,
        },
        "Message": {"PositionReport": {"UserID": 574001234, "Valid": True, "Timestamp": 20}},
    }
    body = (json.dumps(message) + "\n").encode()
    reports = AisStreamMapper().map(_raw_record("aisstream", body, uri="wss://example.org/stream"))

    assert len(reports) == 1
    assert reports[0].record_id.startswith("message:574001234:")
    assert reports[0].latitude == 12.0 and reports[0].longitude == 111.0
    assert not hasattr(reports[0], "observed_at")


def test_aisstream_connector_keeps_api_key_out_of_archived_snapshot():
    message = json.dumps(
        {
            "MessageType": "PositionReport",
            "MetaData": {"MMSI": 574001234, "Latitude": 12.0, "Longitude": 111.0},
            "Message": {"PositionReport": {"UserID": 574001234, "Valid": True}},
        }
    ).encode()

    class FakeSocket:
        def __init__(self):
            self.sent: list[str] = []
            self.frames = [message]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send(self, value: str) -> None:
            self.sent.append(value)

        def recv(self, *, timeout: float) -> bytes:
            del timeout
            return self.frames.pop(0)

    sockets: list[FakeSocket] = []

    def connect(*_args, **_kwargs):
        socket = FakeSocket()
        sockets.append(socket)
        return socket

    now = [0.0]
    connector = AisStreamConnector(
        contract=_contract("aisstream"),
        api_key="secret-key-for-test",
        duration_seconds=15,
        max_messages=1,
        connect=connect,
        monotonic=lambda: now[0],
        sleep=lambda _delay: None,
    )
    page = connector.fetch_page(None)

    assert len(page.records) == 1
    assert b"secret-key-for-test" not in page.records[0].body
    assert "secret-key-for-test" not in page.records[0].source_uri
    assert "secret-key-for-test" not in connector.request_fingerprint
    subscription = json.loads(sockets[0].sent[0])
    assert subscription["BoundingBoxes"] == [[[0.0, 98.0], [25.0, 122.0]]]
    assert subscription["FilterMessageTypes"] == ["PositionReport"]


def test_overpass_connector_posts_bounded_query_and_mapper_keeps_regional_ports():
    body = json.dumps(
        {
            "elements": [
                {
                    "type": "way",
                    "id": 42,
                    "center": {"lat": 10.0, "lon": 106.0},
                    "tags": {"amenity": "port", "name": "Example Port"},
                },
                {
                    "type": "node",
                    "id": 43,
                    "lat": 30.0,
                    "lon": 106.0,
                    "tags": {"harbour": "yes"},
                },
            ]
        }
    ).encode()

    class FakeHttp:
        request = None

        def post(self, url, request_body, *, accept, content_type, headers=None):
            self.request = (url, request_body, accept, content_type, headers)
            return body

    http = FakeHttp()
    connector = OverpassConnector(contract=_contract("openstreetmap"), http=http)
    page = connector.fetch_page(None)
    reports = OverpassMapper().map(page.records[0])

    assert http.request[0] == "https://overpass-api.de/api/interpreter"
    query = urllib.parse.parse_qs(http.request[1].decode())["data"][0]
    assert "(0.0,98.0,12.5,110.0)" in query
    assert "[maxsize:52428800]" in query
    assert not page.complete
    assert page.next_cursor == "1"
    assert len(reports) == 1
    assert reports[0].record_id == "way:42"
    assert reports[0].title.endswith("Example Port")


def test_overpass_pages_are_resumable_and_mapper_enforces_run_wide_cap():
    body = json.dumps(
        {
            "elements": [
                {
                    "type": "node",
                    "id": 42,
                    "lat": 10.0,
                    "lon": 106.0,
                    "tags": {"amenity": "port"},
                },
            ]
        }
    ).encode()

    class FakeHttp:
        def post(self, *_args, **_kwargs):
            return body

    connector = OverpassConnector(contract=_contract("openstreetmap"), http=FakeHttp())
    mapper = OverpassMapper(max_records=2)
    first = connector.fetch_page(None)
    second = connector.fetch_page(first.next_cursor)

    assert not first.complete and first.next_cursor == "1"
    assert not second.complete and second.next_cursor == "2"
    assert connector.fetch_page("3").complete
    assert mapper.map(first.records[0])
    assert mapper.map(second.records[0])
    mapper_with_resumed_cap = OverpassMapper(max_records=2, already_mapped=2)
    assert mapper_with_resumed_cap.map(connector.fetch_page("2").records[0]) == ()


def test_usgs_weekly_feed_maps_only_regional_geojson_features():
    body = json.dumps(
        {
            "type": "FeatureCollection",
            "metadata": {"generated": 12345},
            "features": [
                {
                    "type": "Feature",
                    "id": "vn1",
                    "properties": {
                        "time": 1_700_000_000_000,
                        "mag": 4.2,
                        "place": "South China Sea",
                        "url": "https://earthquake.usgs.gov/event/vn1",
                        "status": "reviewed",
                    },
                    "geometry": {"type": "Point", "coordinates": [112.0, 12.0, 10.0]},
                },
                {
                    "type": "Feature",
                    "id": "outside",
                    "properties": {"place": "outside"},
                    "geometry": {"type": "Point", "coordinates": [130.0, 12.0, 10.0]},
                },
            ],
        }
    ).encode()

    class FakeHttp:
        def get_json(self, url):
            assert url.endswith("all_week.geojson")
            return body, json.loads(body)

    page = UsgsWeeklyConnector(contract=_contract("usgs-earthquakes"), http=FakeHttp()).fetch_page(
        None
    )
    reports = UsgsEarthquakeMapper().map(page.records[0])

    assert len(reports) == 1
    assert reports[0].record_id == "vn1"
    assert '"magnitude": 4.2' in reports[0].text


def test_firms_connector_redacts_map_key_and_mapper_filters_bad_coordinates():
    body = (
        b"latitude,longitude,acq_date,acq_time,satellite,instrument,confidence\n"
        b"12.5,111.0,2026-11-15,1200,N20,VIIRS,n\n"
        b"12.5,130.0,2026-11-15,1201,N20,VIIRS,h\n"
    )

    class FakeHttp:
        url = ""

        def get(self, url, *, accept):
            self.url = url
            assert accept == "text/csv"
            return body

    http = FakeHttp()
    token = "secret-map-key-123456"
    connector = FirmsAreaConnector(
        contract=_contract("nasa-firms"),
        http=http,
        map_key=token,
        snapshot_date=date(2026, 11, 15),
    )
    page = connector.fetch_page(None)
    reports = FirmsHotspotMapper().map(page.records[0])

    assert token in http.url
    assert token not in page.records[0].source_uri
    assert token not in connector.request_fingerprint
    assert len(reports) == 1
    assert reports[0].record_id.startswith("hotspot:")
    assert "source_coordinates" in reports[0].text


def test_firms_http_errors_do_not_expose_path_key():
    token = "secret-map-key-123456"

    class FailingHttp:
        def get(self, url, *, accept):
            del accept
            raise SourceAdapterError(f"HTTP 403 fetching {url}")

    connector = FirmsAreaConnector(
        contract=_contract("nasa-firms"),
        http=FailingHttp(),
        map_key=token,
        snapshot_date=date(2026, 11, 15),
    )

    with pytest.raises(SourceAdapterError, match="check MAP_KEY") as error:
        connector.fetch_page(None)
    assert token not in str(error.value)


def test_gfw_uses_header_auth_and_maps_regional_events_without_identity_claims():
    body = json.dumps(
        {
            "entries": [
                {
                    "id": "event-1",
                    "type": "FISHING",
                    "start": "2026-11-06T00:00:00Z",
                    "end": "2026-11-06T01:00:00Z",
                    "position": {"lat": "12.0", "lon": "111.0"},
                    "vessel": {"id": "provider-vessel", "name": "VESSEL"},
                },
                {
                    "id": "event-outside",
                    "type": "FISHING",
                    "position": {"lat": "12.0", "lon": "130.0"},
                },
            ]
        }
    ).encode()

    class FakeHttp:
        request = None

        def post(self, url, request_body, *, accept, content_type, headers=None):
            self.request = (url, request_body, accept, content_type, headers)
            return body

    http = FakeHttp()
    token = "private-gfw-token"
    connector = GlobalFishingWatchConnector(
        contract=_contract("global-fishing-watch"),
        http=http,
        api_token=token,
        start_date=date(2026, 11, 6),
        end_date=date(2026, 11, 13),
    )
    page = connector.fetch_page(None)
    reports = GlobalFishingWatchMapper().map(page.records[0])

    assert http.request[4] == {"Authorization": f"Bearer {token}"}
    assert token not in connector.request_fingerprint
    assert token not in page.records[0].source_uri
    request_payload = json.loads(http.request[1])
    assert request_payload["geometry"]["coordinates"][0][0] == [98.0, 0.0]
    assert len(reports) == 1
    assert reports[0].record_id == "event-1"
    assert reports[0].locator.endswith("#event-1")


def test_gdacs_connector_preserves_feed_and_maps_regional_alert_episode():
    body = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [109.0, 15.0]},
                    "properties": {
                        "eventtype": "TC",
                        "eventid": 123,
                        "episodeid": 456,
                        "name": "Tropical cyclone report",
                        "alertlevel": "Orange",
                        "fromdate": "2026-11-10T00:00:00Z",
                        "todate": "2026-11-12T00:00:00Z",
                        "url": {"report": "https://www.gdacs.org/report.aspx?eventid=123"},
                    },
                },
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [150.0, 15.0]},
                    "properties": {"eventtype": "TC", "eventid": 999, "episodeid": 1},
                },
            ],
        }
    ).encode()

    class FakeHttp:
        def get_json(self, url):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            assert query["geometryArea"] == ["POLYGON ((98 0, 122 0, 122 25, 98 25, 98 0))"]
            assert query["days"] == ["4"]
            return body, json.loads(body)

    page = GdacsConnector(contract=_contract("gdacs"), http=FakeHttp()).fetch_page(None)
    reports = GdacsEventMapper().map(page.records[0])

    assert len(reports) == 1
    assert reports[0].record_id == "TC:123:456"
    assert reports[0].locator.endswith("eventid=123")
    assert '"alert_level": "Orange"' in reports[0].text
