"""Tests for the sensor/tracking ingestion mapping (Phase 4 vertical)."""

from __future__ import annotations

import json

import pytest
from conftest import CORE_ONTOLOGY, SHAPES_FILE

from foundry.events import EventLog
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline
from foundry.projector import replay_log
from foundry.tracking import FeedRecord, parse_feed


def _pipeline(tmp_path) -> IngestionPipeline:
    return IngestionPipeline(
        identity=IdentityService(),
        log=EventLog(tmp_path / "events.jsonl"),
        ontology_path=CORE_ONTOLOGY,
        shapes_path=SHAPES_FILE,
    )


FEED_LINES = [
    json.dumps(
        {
            "mmsi": "567890123",
            "name": "Tàu tuần tra HQ-272",
            "sensor_id": "RADAR-KQ-01",
            "track_id": "TRK-001",
            "location_uri": "urn:world:location:bench-vungtau",
            "at_time": "2026-09-01T03:00:00Z",
        }
    ),
    json.dumps(
        {
            "mmsi": "567890123",
            "name": "Tàu tuần tra HQ-272",
            "sensor_id": "RADAR-KQ-01",
            "track_id": "TRK-001",
            "location_uri": "urn:world:location:bench-phuquoc",
            "at_time": "2026-09-01T04:00:00Z",
        }
    ),
]


class TestParseFeed:
    def test_parses_normalized_records(self):
        records = parse_feed(tuple(FEED_LINES))
        assert len(records) == 2
        assert records[0].mmsi == "567890123"
        assert records[0].name == "Tàu tuần tra HQ-272"

    def test_rejects_missing_fields(self):
        with pytest.raises(ValueError, match="missing field"):
            parse_feed((json.dumps({"mmsi": "1"}),))

    def test_rejects_non_json_line(self):
        with pytest.raises(ValueError, match="not JSON"):
            parse_feed(("không phải json",))

    def test_rejects_empty_name(self):
        bad = json.loads(FEED_LINES[0])
        bad["name"] = "  "
        with pytest.raises(ValueError, match="empty platform name"):
            parse_feed((json.dumps(bad),))

    def test_feed_records_map_to_events(self):
        records = parse_feed(tuple(FEED_LINES))
        assert all(isinstance(r, FeedRecord) for r in records)
        # each record carries exactly the fields the mapping needs
        for record in records:
            assert record.mmsi and record.name and record.sensor_id
            assert record.track_id and record.location_uri and record.at_time


class TestTrackingVertical:
    @pytest.fixture()
    def pipeline(self, tmp_path) -> IngestionPipeline:
        return _pipeline(tmp_path)

    def _register_platform(self, pipeline):
        return pipeline.ingest_entity(
            name="Tàu tuần tra HQ-272",
            entity_type="Platform",
            external_source="ais-mmsi",
            external_id="567890123",
            source_id="ais-feed",
        )

    def _register_sensor(self, pipeline, platform):
        return pipeline.ingest_sensor(
            sensor_id="RADAR-KQ-01",
            name="Radar KQ-01",
            platform_name="Tàu tuần tra HQ-272",
            source_id="ais-feed",
        )

    def test_sensor_registration_resolves_and_gates(self, pipeline):
        platform = self._register_platform(pipeline)
        result = self._register_sensor(pipeline, platform)
        assert result.accepted
        assert result.event.event_type == "SensorRegistered"
        assert result.event.payload["platform_entity_id"] == platform.canonical_id

    def test_sensor_registration_is_idempotent_by_serial(self, pipeline):
        platform = self._register_platform(pipeline)
        first = self._register_sensor(pipeline, platform)
        second = self._register_sensor(pipeline, platform)
        assert first.canonical_id == second.canonical_id
        assert second.event is not None  # re-registration is a recorded fact

    def test_observation_gates_through_observation_shape(self, pipeline):
        platform = self._register_platform(pipeline)
        sensor = self._register_sensor(pipeline, platform)
        result = pipeline.ingest_observation(
            sensor_entity_id=sensor.canonical_id,
            subject_name="Tàu tuần tra HQ-272",
            subject_entity_id=platform.canonical_id,
            at_time="2026-09-01T03:00:00Z",
            location_uri="urn:world:location:bench-vungtau",
            source_ids=["ais-feed"],
            confidence=0.9,
        )
        assert result.accepted
        assert result.event.event_type == "ObservationRecorded"

    def test_observation_rejects_bad_timestamp(self, pipeline):
        platform = self._register_platform(pipeline)
        sensor = self._register_sensor(pipeline, platform)
        with pytest.raises(ValueError, match="invalid at_time"):
            pipeline.ingest_observation(
                sensor_entity_id=sensor.canonical_id,
                subject_name="Tàu tuần tra HQ-272",
                subject_entity_id=platform.canonical_id,
                at_time="not-a-date",
                location_uri=None,
                source_ids=["ais-feed"],
            )

    def test_track_gates_through_track_shape(self, pipeline):
        platform = self._register_platform(pipeline)
        sensor = self._register_sensor(pipeline, platform)
        obs = pipeline.ingest_observation(
            sensor_entity_id=sensor.canonical_id,
            subject_name="Tàu tuần tra HQ-272",
            subject_entity_id=platform.canonical_id,
            at_time="2026-09-01T03:00:00Z",
            location_uri="urn:world:location:bench-vungtau",
            source_ids=["ais-feed"],
        )
        track = pipeline.ingest_track(
            track_id="TRK-001",
            subject_name="Tàu tuần tra HQ-272",
            subject_entity_id=platform.canonical_id,
            observation_ids=[obs.event.payload["observation_id"]],
            source_ids=["ais-feed"],
        )
        assert track.accepted
        assert track.event.event_type == "TrackObserved"

    def test_track_without_observations_is_rejected(self, pipeline):
        platform = self._register_platform(pipeline)
        track = pipeline.ingest_track(
            track_id="TRK-002",
            subject_name="Tàu tuần tra HQ-272",
            subject_entity_id=platform.canonical_id,
            observation_ids=[],
            source_ids=["ais-feed"],
        )
        assert not track.accepted
        assert "at least one observation" in track.reason


class TestTrackingProjection:
    def test_feed_end_to_end_projection(self, tmp_path):
        pipeline = _pipeline(tmp_path)
        platform = pipeline.ingest_entity(
            name="Tàu tuần tra HQ-272",
            entity_type="Platform",
            external_source="ais-mmsi",
            external_id="567890123",
            source_id="ais-feed",
        )
        sensor = pipeline.ingest_sensor(
            sensor_id="RADAR-KQ-01",
            name="Radar KQ-01",
            platform_name="Tàu tuần tra HQ-272",
            source_id="ais-feed",
        )
        obs_ids = []
        for i, at_time in enumerate(("2026-09-01T03:00:00Z", "2026-09-01T04:00:00Z")):
            obs = pipeline.ingest_observation(
                sensor_entity_id=sensor.canonical_id,
                subject_name="Tàu tuần tra HQ-272",
                subject_entity_id=platform.canonical_id,
                at_time=at_time,
                location_uri=f"urn:world:location:point-{i}",
                source_ids=["ais-feed"],
            )
            obs_ids.append(obs.event.payload["observation_id"])
        pipeline.ingest_track(
            track_id="TRK-001",
            subject_name="Tàu tuần tra HQ-272",
            subject_entity_id=platform.canonical_id,
            observation_ids=obs_ids,
            source_ids=["ais-feed"],
        )

        model, stats = replay_log(pipeline._log)
        # 1 EntityCreated (platform) + 1 SensorRegistered + 2 observations + 1 track
        assert stats.applied == 5
        track = model.get_track("TRK-001")
        assert track is not None
        assert track["entity_id"] == platform.canonical_id
        assert track["observation_ids"] == obs_ids
        assert model.tracks_of(platform.canonical_id)[0]["track_id"] == "TRK-001"
        assert model.stats()["tracks"] == 1

        # deterministic replay
        model_b, stats_b = replay_log(pipeline._log)
        assert stats_b.applied == stats.applied
        assert model_b.get_track("TRK-001") == track

    def test_snapshot_round_trips_tracks(self, tmp_path):
        from foundry.readmodel import ReadModel

        pipeline = _pipeline(tmp_path)
        platform = pipeline.ingest_entity(
            name="Tàu tuần tra HQ-272",
            entity_type="Platform",
            external_source="ais-mmsi",
            external_id="567890123",
            source_id="ais-feed",
        )
        sensor = pipeline.ingest_sensor(
            sensor_id="RADAR-KQ-01",
            name="Radar KQ-01",
            platform_name="Tàu tuần tra HQ-272",
            source_id="ais-feed",
        )
        obs = pipeline.ingest_observation(
            sensor_entity_id=sensor.canonical_id,
            subject_name="Tàu tuần tra HQ-272",
            subject_entity_id=platform.canonical_id,
            at_time="2026-09-01T03:00:00Z",
            location_uri="urn:world:location:p1",
            source_ids=["ais-feed"],
        )
        pipeline.ingest_track(
            track_id="TRK-001",
            subject_name="Tàu tuần tra HQ-272",
            subject_entity_id=platform.canonical_id,
            observation_ids=[obs.event.payload["observation_id"]],
            source_ids=["ais-feed"],
        )
        model, _stats = replay_log(pipeline._log)
        snapshot = tmp_path / "model.pkl"
        model.save_snapshot(snapshot)
        restored = ReadModel.load_snapshot(snapshot)
        assert restored is not None
        assert restored.get_track("TRK-001") == model.get_track("TRK-001")
