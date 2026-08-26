"""Tests for deterministic entity identity resolution."""

from __future__ import annotations

import pytest

from foundry.identity import IdentityService, normalize_name


class TestNormalization:
    def test_case_and_punctuation_insensitive(self):
        assert normalize_name("USS Gerald R. Ford!") == normalize_name("USS Gerald R Ford")
        assert normalize_name("  multiple   spaces ") == "multiple spaces"


class TestResolution:
    @pytest.fixture()
    def service(self) -> IdentityService:
        svc = IdentityService()
        svc.resolve(
            name="USS Gerald R. Ford",
            external_source="usn-hull",
            external_id="CVN-78",
            entity_type="Platform",
        )
        return svc

    def test_external_id_resolves_across_names(self, service):
        result = service.resolve(
            external_source="usn-hull", external_id="CVN-78", entity_type="Platform"
        )
        assert result.method == "external_id"
        assert result.confidence == 1.0
        assert not result.is_new

    def test_exact_alias_match(self, service):
        result = service.resolve(name="USS Gerald R. Ford", entity_type="Platform")
        assert result.method == "alias"
        assert not result.is_new

    def test_similar_variant_is_proposed_for_review_not_merged(self, service):
        result = service.resolve(name="Gerald Ford Carrier", entity_type="Platform")
        assert result.method == "review"
        assert result.canonical_id == ""
        original = service.resolve(name="USS Gerald R. Ford", entity_type="Platform")
        assert original.canonical_id in result.candidates

    def test_unrelated_name_creates_new_entity(self, service):
        result = service.resolve(name="Kilo-class submarine 42", entity_type="Platform")
        assert result.method == "new"
        assert result.is_new
        assert result.canonical_id.startswith("urn:world:entity:")

    def test_review_outcomes_list_every_candidate(self):
        svc = IdentityService()
        svc.resolve(name="Alpha Patrol Unit One", entity_type="Organization")
        # The second unit overlaps "One" lexically, so resolve() routes it to
        # review instead of silently merging; a human confirms it via register.
        review = svc.resolve(name="Alpha Patrol Unit Two", entity_type="Organization")
        assert review.method == "review"
        two_id = f"urn:world:entity:{'2' * 32}"
        svc.register(
            entity_id=two_id,
            entity_type="Organization",
            aliases=["Alpha Patrol Unit Two"],
        )

        result = svc.resolve(name="Alpha Patrol Unit", entity_type="Organization")
        assert result.method == "review"
        assert len(result.candidates) >= 2

    def test_resolve_requires_name_or_external_id(self):
        with pytest.raises(ValueError, match="requires a name or an external_id"):
            IdentityService().resolve(entity_type="Platform")

    def test_register_rejects_type_conflict(self, service):
        canonical = service.resolve(name="USS Gerald R. Ford", entity_type="Platform").canonical_id
        with pytest.raises(ValueError, match="type conflict"):
            service.register(entity_id=canonical, entity_type="Person")
