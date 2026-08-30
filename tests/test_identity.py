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

    def test_trusted_external_id_creates_new_entity_on_fuzzy_hit(self, service):
        """ADR-0006: a supplied external id asserts identity — no fuzzy review.

        Two records with distinct external ids are distinct entities even when
        their names lexically overlap; each binds to its own canonical id.
        """
        result = service.resolve(
            name="Gerald Ford Carrier",
            external_source="naval-registry",
            external_id="NVR-77",
            entity_type="Platform",
        )
        assert result.method == "new"
        assert result.is_new
        assert result.candidates == ()

        # the new entity is resolvable by its external id from now on
        again = service.resolve(
            external_source="naval-registry", external_id="NVR-77", entity_type="Platform"
        )
        assert again.method == "external_id"
        assert again.canonical_id == result.canonical_id

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


class TestFuzzyBlockingIndex:
    """The token blocking index must be an exact match for a full scan."""

    @staticmethod
    def _brute_force_candidates(service: IdentityService, query: str) -> list[tuple[str, float]]:
        tokens = set(normalize_name(query).split())
        found = []
        for alias_norm, cid in service._by_alias.items():
            alias_tokens = set(alias_norm.split())
            if not tokens or not alias_tokens:
                continue
            score = len(tokens & alias_tokens) / min(len(tokens), len(alias_tokens))
            if score >= 0.50:
                found.append((cid, score))
        return sorted(found)

    @pytest.fixture()
    def registry(self) -> IdentityService:
        names = [
            "Hội Chữ thập đỏ Việt Nam",
            "Red Cross of Viet Nam",
            "Viet Nam Red Cross Society",
            "Australian National University",
            "National University of Singapore",
            "National University Hospital",
            "Trường Đại học Quốc gia Hà Nội",
            "Vietnam National University Hanoi",
            "Alpha Patrol Unit One",
            "Alpha Patrol Unit Two",
            "Bravo Recon Team Three",
            "USS Gerald R. Ford",
            "Gerald R. Ford Carrier",
            "USS Gerald R Ford Jr",
            "Kilo-class submarine 42",
            "Kilo-class submarine 43",
            " Ministry of National Defence ",
            "Bộ Quốc phòng",
            "Ministry of Public Security!!!",
            "7th Naval Region",
            "Naval Region 7 Command",
            "Hải quân nhân dân Việt Nam",
            "Vietnam People's Navy",
            "Đại học Quốc gia Úc",
        ]
        svc = IdentityService()
        for i, name in enumerate(names):
            svc.register(
                entity_id=f"urn:world:entity:{i:032x}",
                entity_type="Organization",
                aliases=[name],
            )
        return svc

    @pytest.mark.parametrize(
        "query",
        [
            "Viet Nam Red Cross",
            "Red Cross of Viet Nam Society",
            "National University",
            "University Hospital Singapore",
            "Alpha Patrol Unit",
            "USS Gerald Ford",
            "Gerald Ford",
            "Kilo-class submarine",
            "Ministry of Defence",
            "Naval Region",
            "Vietnam People's Navy Command",
            "Đại học Quốc gia",
            "completely unrelated words",
            "Xyz",
        ],
    )
    def test_candidates_match_brute_force_scan(self, registry, query):
        expected = self._brute_force_candidates(registry, query)
        actual = sorted(registry._fuzzy_candidates(normalize_name(query)))
        assert actual == expected

    def test_empty_query_returns_no_candidates(self):
        svc = IdentityService()
        svc.register(entity_id="urn:world:entity:a", entity_type="Person", aliases=["..."])
        assert svc._fuzzy_candidates("") == []
