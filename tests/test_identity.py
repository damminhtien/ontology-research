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


class TestPureLookup:
    def test_lookup_miss_does_not_mutate_registry(self):
        svc = IdentityService()
        result = svc.lookup(name="Never Seen Before", entity_type="Platform")
        assert result.method == "miss"
        assert result.canonical_id == ""
        assert svc._records == {}  # pure: no entity minted, nothing bound
        # resolve() afterwards still creates the entity
        resolved = svc.resolve(name="Never Seen Before", entity_type="Platform")
        assert resolved.method == "new" and resolved.is_new

    def test_lookup_requires_name_or_external_id(self):
        with pytest.raises(ValueError, match="requires a name or an external_id"):
            IdentityService().lookup()

    def test_lookup_reports_hit_methods_without_minting(self):
        svc = IdentityService()
        created = svc.resolve(
            name="USS Gerald R. Ford",
            external_source="usn-hull",
            external_id="CVN-78",
            entity_type="Platform",
        )
        external = svc.lookup(
            external_source="usn-hull", external_id="CVN-78", entity_type="Platform"
        )
        assert external.method == "external_id"
        assert external.canonical_id == created.canonical_id
        alias = svc.lookup(name="USS Gerald R. Ford", entity_type="Platform")
        assert alias.method == "alias"
        assert alias.canonical_id == created.canonical_id
        # without a type filter the alias still resolves (introspection use)
        untyped = svc.lookup(name="USS Gerald R. Ford")
        assert untyped.canonical_id == created.canonical_id


class TestTypeAwareLookup:
    def test_same_name_across_types_are_distinct_entities(self):
        svc = IdentityService()
        org = svc.resolve(
            name="Hải quân nhân dân Việt Nam", entity_type="Organization"
        ).canonical_id
        # the same surface name as a Platform must NOT hit the Organization
        platform = svc.resolve(name="Hải quân nhân dân Việt Nam", entity_type="Platform")
        assert platform.method == "new"
        assert platform.canonical_id != org
        # each type resolves back to its own entity
        assert (
            svc.lookup(name="Hải quân nhân dân Việt Nam", entity_type="Organization").canonical_id
            == org
        )
        assert (
            svc.lookup(name="Hải quân nhân dân Việt Nam", entity_type="Platform").canonical_id
            == platform.canonical_id
        )


class TestAliasMultimap:
    def test_alias_collision_is_ambiguous_not_first_wins(self):
        a = "urn:world:entity:" + "a" * 32
        b = "urn:world:entity:" + "b" * 32
        svc = IdentityService()
        svc.register(entity_id=a, entity_type="Organization", aliases=["Trung đoàn 101"])
        svc.register(entity_id=b, entity_type="Organization", aliases=["Trung đoàn 101"])

        found = svc.lookup(name="Trung đoàn 101", entity_type="Organization")
        assert found.method == "ambiguous"
        assert found.canonical_id == ""
        assert set(found.candidates) == {a, b}
        # resolve() routes the collision to review instead of guessing
        resolved = svc.resolve(name="Trung đoàn 101", entity_type="Organization")
        assert resolved.method == "review"
        assert set(resolved.candidates) == {a, b}

    def test_typed_lookup_disambiguates_cross_type_sharing(self):
        a = "urn:world:entity:" + "a" * 32
        b = "urn:world:entity:" + "b" * 32
        svc = IdentityService()
        svc.register(entity_id=a, entity_type="Organization", aliases=["Region 7"])
        svc.register(entity_id=b, entity_type="Platform", aliases=["Region 7"])
        assert svc.lookup(name="Region 7", entity_type="Organization").canonical_id == a
        assert svc.lookup(name="Region 7", entity_type="Platform").canonical_id == b

    def test_len_excludes_merged_away_entities(self):
        a = "urn:world:entity:" + "a" * 32
        b = "urn:world:entity:" + "b" * 32
        svc = IdentityService()
        svc.register(entity_id=a, entity_type="Organization", aliases=["Alpha"])
        svc.register(entity_id=b, entity_type="Organization", aliases=["Beta"])
        assert len(svc) == 2
        svc.merge_entities(a, b)
        assert len(svc) == 1  # b is a redirect, not a live entity


class TestMultiValuedExternalIds:
    def test_entity_holds_several_ids_per_source(self):
        svc = IdentityService()
        entity = svc.resolve(
            name="Hội Chữ thập đỏ Việt Nam",
            external_source="wikidata",
            external_id="Q108321",
            entity_type="Organization",
        ).canonical_id
        svc.add_external_id(entity, "wikidata", "Q999999")  # cross-walk/reassignment

        _, _, external_ids = svc.identity(entity)
        assert external_ids["wikidata"] == frozenset({"Q108321", "Q999999"})
        for qid in ("Q108321", "Q999999"):
            hit = svc.lookup(
                external_source="wikidata", external_id=qid, entity_type="Organization"
            )
            assert hit.method == "external_id"
            assert hit.canonical_id == entity

    def test_register_accepts_iterable_values(self):
        svc = IdentityService()
        entity = "urn:world:entity:" + "c" * 32
        svc.register(
            entity_id=entity,
            entity_type="Person",
            external_ids={"mnis": ["1234", "5678"]},
        )
        _, _, external_ids = svc.identity(entity)
        assert external_ids["mnis"] == frozenset({"1234", "5678"})


class TestFuzzyBlockingIndex:
    """The token blocking index must be an exact match for a full scan."""

    @staticmethod
    def _brute_force_candidates(service: IdentityService, query: str) -> list[tuple[str, float]]:
        tokens = set(normalize_name(query).split())
        best: dict[str, float] = {}
        for alias_norm, owners in service._by_alias.items():
            alias_tokens = set(alias_norm.split())
            if not tokens or not alias_tokens:
                continue
            score = len(tokens & alias_tokens) / min(len(tokens), len(alias_tokens))
            if score < 0.50:
                continue
            for owner in owners:
                if score > best.get(owner, 0.0):
                    best[owner] = score
        return sorted(best.items())

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
