#!/usr/bin/env python3
"""Compare the current identity resolver with offline Splink suggestions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import random
import re
import sys
import tempfile
import threading
import time
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.identity import IdentityService, normalize_name  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "benchmarks/evaluations/febrl4_sources.json"
DEFAULT_DATA_DIR = REPO_ROOT / "build/splink-evaluation/datasets"
DEFAULT_OUTPUT = REPO_ROOT / "build/splink-evaluation/report.json"
LEFT_TABLE = "records_a"
RIGHT_TABLE = "records_b"
REVIEW_BUDGET_PER_RECORD = 5
FUZZY_REVIEW_THRESHOLD = 0.50
SEED = 42

Pair = tuple[str, str]


@dataclass(frozen=True)
class Candidate:
    """One candidate pair and its optional model score."""

    left_key: str
    right_key: str
    score: float | None = None

    @property
    def pair(self) -> Pair:
        """Return the record-key pair."""
        return self.left_key, self.right_key


@dataclass(frozen=True)
class DatasetBundle:
    """Feature rows with labels and safety metadata kept outside the features."""

    name: str
    profile: str
    left: tuple[dict[str, Any], ...]
    right: tuple[dict[str, Any], ...]
    truth_by_key: dict[str, str]
    type_by_key: dict[str, str]
    trusted_id_by_key: dict[str, str | None]
    resolver_name_by_key: dict[str, str | None]
    resolver_aliases_by_key: dict[str, tuple[str, ...]]
    feature_columns: tuple[str, ...]
    blocking_only_columns: tuple[str, ...] = ()
    synthetic_cannot_link: bool = False


@dataclass(frozen=True)
class DataSplit:
    """One entity-disjoint train, validation, or test split."""

    name: str
    left: tuple[dict[str, Any], ...]
    right: tuple[dict[str, Any], ...]
    truth_pairs: frozenset[Pair]
    cannot_link_pairs: frozenset[Pair]
    truth_by_key: dict[str, str]
    type_by_key: dict[str, str]
    trusted_id_by_key: dict[str, str | None]
    resolver_name_by_key: dict[str, str | None]
    resolver_aliases_by_key: dict[str, tuple[str, ...]]
    feature_columns: tuple[str, ...]
    blocking_only_columns: tuple[str, ...]


@dataclass(frozen=True)
class Profile:
    """Splink comparisons and prediction/training blocks for one dataset."""

    comparisons: tuple[Any, ...]
    prediction_blocks: tuple[Any, ...]
    training_blocks: tuple[Any, ...]
    blocking_only_columns: tuple[str, ...] = ()


class PeakRssSampler:
    """Sample process RSS while one measured stage is running."""

    def __init__(self, interval_seconds: float = 0.025) -> None:
        """Initialize process sampling at the given interval."""
        self.interval_seconds = interval_seconds
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: Any = None

    def __enter__(self) -> PeakRssSampler:
        """Start sampling and return this context manager."""
        try:
            import psutil
        except ImportError as exc:  # pragma: no cover - guarded by isolated env
            raise RuntimeError("install requirements/evaluation-splink.txt to measure RSS") from exc
        self._process = psutil.Process()
        self._sample()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop sampling and collect the final process RSS value."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._sample()

    def _sample(self) -> None:
        with suppress(Exception):  # pragma: no cover - process shutdown race
            self.peak_bytes = max(self.peak_bytes, self._process.memory_info().rss)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()


def _measure(function: Callable[[], Any]) -> tuple[Any, float, int]:
    """Run a stage and return its output, wall time, and sampled peak RSS."""
    with PeakRssSampler() as sampler:
        started = time.perf_counter()
        result = function()
        elapsed = time.perf_counter() - started
    return result, elapsed, sampler.peak_bytes


def _load_manifest() -> dict[str, Any]:
    """Load the checked-in FEBRL4 source and checksum manifest."""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def fetch_febrl4(data_dir: Path) -> dict[str, str]:
    """Download both pinned FEBRL4 files and verify their SHA-256 digests."""
    manifest = _load_manifest()
    data_dir.mkdir(parents=True, exist_ok=True)
    downloaded: dict[str, str] = {}
    for filename, entry in manifest["files"].items():
        destination = data_dir / filename
        with tempfile.NamedTemporaryFile(dir=data_dir, delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            urllib.request.urlretrieve(entry["url"], temporary_path)
            digest = _sha256(temporary_path)
            if digest != entry["sha256"]:
                raise ValueError(
                    f"checksum mismatch for {filename}: expected {entry['sha256']}, got {digest}"
                )
            temporary_path.replace(destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        downloaded[filename] = digest
    return downloaded


def _sha256(path: Path) -> str:
    """Hash one file without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_febrl4(data_dir: Path) -> dict[str, Path]:
    """Return verified data paths; benchmark execution itself stays offline."""
    manifest = _load_manifest()
    paths: dict[str, Path] = {}
    for filename, entry in manifest["files"].items():
        path = data_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"missing {path}; run `make splink-fetch` first")
        digest = _sha256(path)
        if digest != entry["sha256"]:
            raise ValueError(
                f"checksum mismatch for {path}: expected {entry['sha256']}, got {digest}"
            )
        paths[filename] = path
    return paths


def _synthetic_dataset(entity_count: int, seed: int) -> DatasetBundle:
    """Create reproducible multilingual aliases, hard negatives, and missing IDs."""
    if entity_count < 30:
        raise ValueError("synthetic entity count must be at least 30")
    names = (
        ("Nguyễn Văn An", "Nguyen Van An"),
        ("Máría de la Cruz", "Maria de la Cruz"),
        ("José Álvarez", "Jose Alvarez"),
        ("İpek Şahin", "Ipek Sahin"),
        ("François Le Blanc", "Francois Leblanc"),
        ("Thảo Nguyễn", "Thao Nguyen"),
        ("Łukasz Żółć", "Lukasz Zolc"),
        ("أحمد الشافعي", "Ahmed El Shafei"),
        ("Sarah Ahmed", "Sara Ahmed"),
        ("Alex Morgan", "Alex Morgan"),
        ("Jordan Lee", "Jordan Lee"),
        ("Taylor Chen", "Taylor Chen"),
    )
    entity_types = ("Organization", "Facility", "Platform")
    regions = ("SG", "AU", "VN", "GB", "MY", "CA")
    rng = random.Random(seed)
    left: list[dict[str, Any]] = []
    right: list[dict[str, Any]] = []
    truth: dict[str, str] = {}
    types: dict[str, str] = {}
    trusted_ids: dict[str, str | None] = {}
    resolver_names: dict[str, str | None] = {}
    aliases_by_key: dict[str, tuple[str, ...]] = {}

    for index in range(entity_count):
        entity_key = f"synthetic-entity-{index:05d}"
        primary, alternate = names[index % len(names)]
        entity_type = entity_types[index % len(entity_types)]
        region = regions[(index // len(names)) % len(regions)]
        left_key = f"syn-a-{index:05d}"
        right_key = f"syn-b-{index:05d}"
        left_name: str | None = primary
        left_alias = alternate
        right_name: str | None = alternate
        right_alias = primary
        if right_name is not None and rng.random() < 0.28:
            right_name = _typo(right_name, rng)
        if rng.random() < 0.16:
            right_alias = _typo(right_alias, rng)
        if rng.random() < 0.08:
            left_name = None
        if rng.random() < 0.08:
            right_name = None
        trusted_id = f"syn-id-{index:05d}" if index % 3 != 0 else None

        left.append({"row_key": left_key, "name": left_name, "alias": left_alias, "region": region})
        right.append(
            {"row_key": right_key, "name": right_name, "alias": right_alias, "region": region}
        )
        for row_key in (left_key, right_key):
            truth[row_key] = entity_key
            types[row_key] = entity_type
            trusted_ids[row_key] = trusted_id
            aliases_by_key[row_key] = (primary, alternate)
        resolver_names[left_key] = left_name
        resolver_names[right_key] = right_name

    return DatasetBundle(
        name="synthetic",
        profile="synthetic",
        left=tuple(left),
        right=tuple(right),
        truth_by_key=truth,
        type_by_key=types,
        trusted_id_by_key=trusted_ids,
        resolver_name_by_key=resolver_names,
        resolver_aliases_by_key=aliases_by_key,
        feature_columns=("name", "alias", "region"),
        synthetic_cannot_link=True,
    )


def _typo(value: str, rng: random.Random) -> str:
    """Delete one seeded random character from a synthetic alias."""
    positions = [index for index, character in enumerate(value) if character.isalpha()]
    if len(positions) < 4:
        return value
    position = rng.choice(positions)
    return value[:position] + value[position + 1 :]


def _febrl4_dataset(data_dir: Path) -> DatasetBundle:
    """Read FEBRL4 while keeping source identifiers exclusively as labels/keys."""
    paths = _require_febrl4(data_dir)
    feature_columns = (
        "given_name",
        "surname",
        "date_of_birth",
        "postcode",
        "street_number",
        "state",
    )
    blocking_only_columns = ("address_1",)
    left: list[dict[str, Any]] = []
    right: list[dict[str, Any]] = []
    truth: dict[str, str] = {}
    types: dict[str, str] = {}
    trusted_ids: dict[str, str | None] = {}
    resolver_names: dict[str, str | None] = {}
    aliases_by_key: dict[str, tuple[str, ...]] = {}

    for side, filename, records in (
        ("a", "dataset4a.csv", left),
        ("b", "dataset4b.csv", right),
    ):
        with paths[filename].open(encoding="utf-8", newline="") as source:
            for row_index, raw in enumerate(csv.DictReader(source, skipinitialspace=True)):
                row = {key.strip(): (value or "").strip() for key, value in raw.items()}
                source_record_id = row.pop("rec_id")
                match = re.fullmatch(r"rec-(\d+)-(?:org|dup-\d+)", source_record_id)
                if match is None:
                    raise ValueError(f"unexpected FEBRL4 row id format: {source_record_id!r}")
                truth_id = f"rec-{match.group(1)}"
                row_key = f"febrl-{side}-{row_index:05d}"
                name = " ".join(
                    part for part in (row.get("given_name"), row.get("surname")) if part
                )
                trusted_id = row.pop("soc_sec_id", "") or None
                row.pop("cluster", None)
                input_columns = (*feature_columns, *blocking_only_columns)
                feature_row = {column: row.get(column) or None for column in input_columns}
                records.append({"row_key": row_key, **feature_row})
                truth[row_key] = truth_id
                types[row_key] = "Person"
                trusted_ids[row_key] = trusted_id
                resolver_names[row_key] = name or None
                aliases_by_key[row_key] = (name,) if name else ()

    if len(left) != 5_000 or len(right) != 5_000:
        raise ValueError(
            f"FEBRL4 files must have 5,000 rows each, got {len(left)} and {len(right)}"
        )
    left_entities = {truth[row["row_key"]] for row in left}
    right_entities = {truth[row["row_key"]] for row in right}
    if len(left_entities) != 5_000 or left_entities != right_entities:
        raise ValueError("FEBRL4 rows must form 5,000 unique one-to-one entity pairs")
    return DatasetBundle(
        name="febrl4",
        profile="febrl4",
        left=tuple(left),
        right=tuple(right),
        truth_by_key=truth,
        type_by_key=types,
        trusted_id_by_key=trusted_ids,
        resolver_name_by_key=resolver_names,
        resolver_aliases_by_key=aliases_by_key,
        feature_columns=feature_columns,
        blocking_only_columns=blocking_only_columns,
    )


def _make_split(bundle: DatasetBundle, split_name: str, entity_ids: set[str]) -> DataSplit:
    """Build a split with no entity crossing its boundary."""
    left = tuple(row for row in bundle.left if bundle.truth_by_key[row["row_key"]] in entity_ids)
    right = tuple(row for row in bundle.right if bundle.truth_by_key[row["row_key"]] in entity_ids)
    right_by_truth: dict[str, list[str]] = defaultdict(list)
    for row in right:
        right_by_truth[bundle.truth_by_key[row["row_key"]]].append(row["row_key"])
    truth_pairs: set[Pair] = set()
    for row in left:
        truth_id = bundle.truth_by_key[row["row_key"]]
        matches = right_by_truth.get(truth_id, [])
        if len(matches) != 1:
            raise ValueError(f"expected one-to-one FEBRL/synthetic truth for {truth_id}")
        truth_pairs.add((row["row_key"], matches[0]))

    cannot_link_pairs: set[Pair] = set()
    if bundle.synthetic_cannot_link:
        right_by_alias: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in right:
            aliases = bundle.resolver_aliases_by_key[row["row_key"]]
            right_by_alias[tuple(sorted(normalize_name(alias) for alias in aliases))].append(row)
        for row in left:
            left_key = row["row_key"]
            aliases = bundle.resolver_aliases_by_key[left_key]
            candidates = right_by_alias[tuple(sorted(normalize_name(alias) for alias in aliases))]
            for candidate in candidates:
                right_key = candidate["row_key"]
                if (
                    bundle.truth_by_key[right_key] != bundle.truth_by_key[left_key]
                    and bundle.type_by_key[right_key] == bundle.type_by_key[left_key]
                ):
                    cannot_link_pairs.add((left_key, right_key))
                    break

    keys = {row["row_key"] for row in (*left, *right)}
    return DataSplit(
        name=split_name,
        left=left,
        right=right,
        truth_pairs=frozenset(truth_pairs),
        cannot_link_pairs=frozenset(cannot_link_pairs),
        truth_by_key={key: bundle.truth_by_key[key] for key in keys},
        type_by_key={key: bundle.type_by_key[key] for key in keys},
        trusted_id_by_key={key: bundle.trusted_id_by_key[key] for key in keys},
        resolver_name_by_key={key: bundle.resolver_name_by_key[key] for key in keys},
        resolver_aliases_by_key={key: bundle.resolver_aliases_by_key[key] for key in keys},
        feature_columns=bundle.feature_columns,
        blocking_only_columns=bundle.blocking_only_columns,
    )


def split_by_entity(bundle: DatasetBundle, seed: int = SEED) -> dict[str, DataSplit]:
    """Partition whole entities 60/20/20 with a fixed shuffle seed."""
    entity_ids = sorted({bundle.truth_by_key[key] for key in bundle.truth_by_key})
    if len(entity_ids) < 10:
        raise ValueError("at least 10 ground-truth entities are needed for disjoint splits")
    random.Random(seed).shuffle(entity_ids)
    train_end = int(len(entity_ids) * 0.60)
    validation_end = int(len(entity_ids) * 0.80)
    partitions = {
        "train": set(entity_ids[:train_end]),
        "validation": set(entity_ids[train_end:validation_end]),
        "test": set(entity_ids[validation_end:]),
    }
    result = {name: _make_split(bundle, name, values) for name, values in partitions.items()}
    split_truth = [
        {result[name].truth_by_key[key] for key in result[name].truth_by_key}
        for name in ("train", "validation", "test")
    ]
    if (
        split_truth[0] & split_truth[1]
        or split_truth[0] & split_truth[2]
        or split_truth[1] & split_truth[2]
    ):
        raise AssertionError("ground-truth entity leaked across train/validation/test")
    return result


def _current_resolver_candidates(split: DataSplit) -> list[Candidate]:
    """Generate proposals using the existing resolver and an isolated memory store."""
    service = IdentityService()
    for row in split.right:
        row_key = row["row_key"]
        trusted_id = split.trusted_id_by_key[row_key]
        service.register(
            entity_id=row_key,
            entity_type=split.type_by_key[row_key],
            aliases=list(split.resolver_aliases_by_key[row_key]),
            external_ids={"benchmark": trusted_id} if trusted_id else None,
        )

    candidates: list[Candidate] = []
    for row in split.left:
        left_key = row["row_key"]
        name = split.resolver_name_by_key[left_key]
        trusted_id = split.trusted_id_by_key[left_key]
        entity_type = split.type_by_key[left_key]
        if not name and not trusted_id:
            continue
        lookup = service.lookup(
            name=name,
            external_source="benchmark",
            external_id=trusted_id,
            entity_type=entity_type,
        )
        if lookup.method == "external_id" or lookup.method == "alias":
            owners = (lookup.canonical_id,)
        elif lookup.method == "ambiguous":
            owners = lookup.candidates
        elif trusted_id is None and name:
            owners = tuple(
                entity_id
                for entity_id, _score in service._fuzzy_candidates(
                    normalize_name(name), entity_type
                )
            )
        else:
            # Current policy mints a new entity after an asserted ID miss; it
            # deliberately does not fall back to fuzzy candidates in that case.
            owners = ()
        candidates.extend(Candidate(left_key, owner) for owner in sorted(set(owners)))
    return candidates


def _splink_profile(profile_name: str) -> Profile:
    """Return feature comparisons and blocks without label or ID columns."""
    import splink.comparison_library as cl
    from splink import block_on

    if profile_name == "synthetic":
        comparisons = (
            cl.NameComparison("name"),
            cl.NameComparison("alias"),
            cl.ExactMatch("region").configure(term_frequency_adjustments=True),
        )
        cross_alias_blocks = ("l.name = r.alias", "l.alias = r.name")
        return Profile(
            comparisons=comparisons,
            prediction_blocks=(
                block_on("name"),
                block_on("alias"),
                *cross_alias_blocks,
            ),
            training_blocks=(block_on("name"), block_on("alias"), block_on("region")),
        )
    if profile_name == "febrl4":
        comparisons = (
            cl.NameComparison("given_name"),
            cl.NameComparison("surname"),
            cl.DateOfBirthComparison(
                "date_of_birth",
                input_is_string=True,
                datetime_format="%Y%m%d",
                invalid_dates_as_null=True,
            ),
            cl.DamerauLevenshteinAtThresholds("postcode", [1, 2]),
            cl.ExactMatch("street_number").configure(term_frequency_adjustments=True),
            cl.ExactMatch("state").configure(term_frequency_adjustments=True),
        )
        prediction_blocks = (
            block_on("given_name", "surname"),
            block_on("date_of_birth", "postcode"),
            block_on("state", "address_1"),
            block_on("surname", "postcode"),
        )
        training_blocks = (
            block_on("given_name", "surname"),
            block_on("date_of_birth", "postcode"),
            block_on("state", "address_1"),
        )
        return Profile(comparisons, prediction_blocks, training_blocks, ("address_1",))
    raise ValueError(f"unknown Splink profile: {profile_name}")


def _settings(profile: Profile):
    """Create repeatable link-only settings for training or prediction."""
    from splink import SettingsCreator

    return SettingsCreator(
        unique_id_column_name="row_key",
        link_type="link_only",
        comparisons=list(profile.comparisons),
        blocking_rules_to_generate_predictions=list(profile.prediction_blocks),
        probability_two_random_records_match=0.0001,
        max_iterations=25,
    )


def _register_split(api: Any, split: DataSplit):
    """Register only record keys and descriptive scoring columns with DuckDB."""
    left = api.register(list(split.left), dataset_display_name=LEFT_TABLE)
    right = api.register(list(split.right), dataset_display_name=RIGHT_TABLE)
    return left, right


def _unestimated_parameters(model_path: Path) -> list[str]:
    """List non-null comparison levels missing an estimated m or u value."""
    settings = json.loads(model_path.read_text(encoding="utf-8"))
    missing: list[str] = []
    for comparison in settings["comparisons"]:
        comparison_name = comparison["output_column_name"]
        for level in comparison["comparison_levels"]:
            if level.get("is_null_level"):
                continue
            for parameter in ("m_probability", "u_probability"):
                if parameter not in level:
                    label = level.get("label_for_charts", level.get("sql_condition", "level"))
                    missing.append(f"{comparison_name}/{label}/{parameter}")
    return missing


def _train_model(
    split: DataSplit,
    profile_name: str,
    model_path: Path,
    max_pairs: int,
    seed: int,
) -> tuple[float, int]:
    """Train unsupervised parameters on the training entities only."""
    from splink import DuckDBAPI, Linker

    profile = _splink_profile(profile_name)

    def train() -> None:
        api = DuckDBAPI()
        dataframes = _register_split(api, split)
        linker = Linker(dataframes, _settings(profile), log_level=40)
        possible_pairs = len(split.left) * len(split.right)
        linker.training.estimate_u_using_random_sampling(
            max_pairs=min(max_pairs, possible_pairs), seed=seed
        )
        for rule in profile.training_blocks:
            linker.training.estimate_parameters_using_expectation_maximisation(rule)
        model_path.parent.mkdir(parents=True, exist_ok=True)
        linker.misc.save_model_to_json(str(model_path), overwrite=True)
        missing = _unestimated_parameters(model_path)
        if missing:
            raise RuntimeError(
                "Splink left comparison parameters unestimated: " + "; ".join(missing)
            )

    _result, elapsed, peak_rss = _measure(train)
    return elapsed, peak_rss


def _score_with_splink(
    split: DataSplit,
    profile_name: str,
    model_path: Path,
    resolver_pairs: list[Candidate] | None = None,
) -> tuple[list[Candidate], float, int]:
    """Run Splink's blocking or score a supplied resolver candidate set."""
    from splink import DuckDBAPI, Linker

    if resolver_pairs is not None and not resolver_pairs:
        return [], 0.0, 0

    def predict() -> list[Candidate]:
        api = DuckDBAPI()
        dataframes = _register_split(api, split)
        linker = Linker(dataframes, str(model_path), log_level=40)
        if resolver_pairs is not None:
            pair_rows = [
                {
                    "match_key": "resolver-candidates",
                    "join_key_l": f"{LEFT_TABLE}-__-{candidate.left_key}",
                    "join_key_r": f"{RIGHT_TABLE}-__-{candidate.right_key}",
                }
                for candidate in resolver_pairs
            ]
            blocked_pairs = api.register(pair_rows, dataset_display_name="resolver_pairs")
            linker.table_management.register_blocked_pairs_for_predict(blocked_pairs)
        scored = linker.inference.predict(warning_mode="never").as_record_list()
        return [
            Candidate(
                str(row["row_key_l"]),
                str(row["row_key_r"]),
                float(row["match_probability"]),
            )
            for row in scored
        ]

    candidates, elapsed, peak_rss = _measure(predict)
    return candidates, elapsed, peak_rss


def _filter_safe(candidates: list[Candidate], split: DataSplit) -> tuple[list[Candidate], int]:
    """Drop type-mismatched and explicitly forbidden pairs before review output."""
    safe: dict[Pair, Candidate] = {}
    dropped = 0
    for candidate in candidates:
        pair = candidate.pair
        if (
            split.type_by_key[pair[0]] != split.type_by_key[pair[1]]
            or pair in split.cannot_link_pairs
        ):
            dropped += 1
        else:
            safe[pair] = candidate
    return list(safe.values()), dropped


def _ranked_top_k(
    candidates: list[Candidate], budget: int, *, stable_order: bool = False
) -> list[Candidate]:
    """Take at most ``budget`` candidates per source record."""
    by_left: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_left[candidate.left_key].append(candidate)
    result: list[Candidate] = []
    for left_key in sorted(by_left):
        if stable_order:
            ranked = sorted(by_left[left_key], key=lambda item: item.right_key)
        else:
            ranked = sorted(
                by_left[left_key],
                key=lambda item: (
                    -(item.score if item.score is not None else -1.0),
                    item.right_key,
                ),
            )
        result.extend(ranked[:budget])
    return result


def _recommendations(
    candidates: list[Candidate],
    threshold: float | None,
    budget: int,
    *,
    stable_order: bool = False,
) -> list[Candidate]:
    """Apply a validation-locked score cutoff and the common per-record budget."""
    if threshold is None and not stable_order:
        return []
    if not stable_order and threshold is not None:
        candidates = [
            candidate
            for candidate in candidates
            if candidate.score is not None and candidate.score >= threshold
        ]
    return _ranked_top_k(candidates, budget, stable_order=stable_order)


def _choose_threshold(
    candidates: list[Candidate], truth_pairs: frozenset[Pair], budget: int
) -> float | None:
    """Choose the validation cutoff that maximizes pairwise F1 under the budget."""
    scores = sorted({item.score for item in candidates if item.score is not None}, reverse=True)
    if not scores:
        return None
    thresholds = [math.nextafter(scores[0], math.inf), *scores]
    best_threshold: float | None = None
    best_key = (-1.0, -1.0, -1.0, -math.inf)
    for threshold in thresholds:
        suggested = _recommendations(candidates, threshold, budget)
        predicted = {item.pair for item in suggested}
        true_positive = len(predicted & truth_pairs)
        precision = true_positive / len(predicted) if predicted else 0.0
        recall = true_positive / len(truth_pairs) if truth_pairs else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        key = (f1, precision, recall, threshold)
        if key > best_key:
            best_key = key
            best_threshold = threshold
    return float(f"{best_threshold:.10g}") if best_threshold is not None else None


def _has_missing_features(record: dict[str, Any] | None, feature_columns: tuple[str, ...]) -> bool:
    """Return true when at least one model feature is empty for one record."""
    if record is None:
        return True
    return any(
        record.get(column) is None or not str(record.get(column)).strip()
        for column in feature_columns
    )


def _evaluate_test(
    split: DataSplit,
    candidates: list[Candidate],
    filtered_count: int,
    threshold: float | None,
    *,
    stable_order: bool = False,
) -> dict[str, Any]:
    """Summarize candidate recall, top-five recall, and safe pair suggestions."""
    truth = set(split.truth_pairs)
    candidate_pairs = {candidate.pair for candidate in candidates}
    top_five = {
        candidate.pair for candidate in _ranked_top_k(candidates, 5, stable_order=stable_order)
    }
    suggested = _recommendations(
        candidates,
        threshold,
        REVIEW_BUDGET_PER_RECORD,
        stable_order=stable_order,
    )
    suggested_pairs = {candidate.pair for candidate in suggested}
    true_positive = len(truth & suggested_pairs)
    false_suggestions = len(suggested_pairs - truth)
    precision = true_positive / len(suggested_pairs) if suggested_pairs else 0.0
    recall = true_positive / len(truth) if truth else 0.0
    left_rows = {row["row_key"]: row for row in split.left}
    right_rows = {row["row_key"]: row for row in split.right}
    descriptive_columns = (*split.feature_columns, *split.blocking_only_columns)
    blocking_misses = truth - candidate_pairs
    ranking_misses = (truth & candidate_pairs) - suggested_pairs

    def has_missing_data(pair: Pair) -> bool:
        return _has_missing_features(
            left_rows.get(pair[0]), descriptive_columns
        ) or _has_missing_features(right_rows.get(pair[1]), descriptive_columns)

    blocking_missing_data_misses = {pair for pair in blocking_misses if has_missing_data(pair)}
    ranking_missing_data_misses = {pair for pair in ranking_misses if has_missing_data(pair)}
    missing_data_misses = blocking_missing_data_misses | ranking_missing_data_misses
    violations = sum(
        split.type_by_key[left_key] != split.type_by_key[right_key]
        or (left_key, right_key) in split.cannot_link_pairs
        for left_key, right_key in suggested_pairs
    )
    if violations:
        raise AssertionError("unsafe pair survived the pre-output safety filter")
    return {
        "test_entities": len(truth),
        "candidate_pairs_after_safety_filter": len(candidate_pairs),
        "candidate_recall": round(len(truth & candidate_pairs) / len(truth), 6) if truth else 0.0,
        "recall_at_5": round(len(truth & top_five) / len(truth), 6) if truth else 0.0,
        "review_budget_per_source_record": REVIEW_BUDGET_PER_RECORD,
        "suggestions": len(suggested_pairs),
        "true_suggestions": true_positive,
        "false_suggestions": false_suggestions,
        "pairwise_precision": round(precision, 6),
        "pairwise_recall": round(recall, 6),
        "blocking_misses": len(blocking_misses),
        "blocking_misses_with_missing_features": len(blocking_missing_data_misses),
        "ranking_or_threshold_misses": len(ranking_misses),
        "ranking_misses_with_missing_features": len(ranking_missing_data_misses),
        "missing_data_affected_misses": len(missing_data_misses),
        "unsafe_candidates_removed_before_scoring_or_suggestion": filtered_count,
        "unsafe_suggestions": violations,
        "threshold": threshold,
    }


def _run_baseline(split: DataSplit) -> tuple[list[Candidate], float, int]:
    """Measure the current resolver as a pure, isolated candidate generator."""
    candidates, elapsed, peak_rss = _measure(lambda: _current_resolver_candidates(split))
    return candidates, elapsed, peak_rss


def _evaluate_dataset(
    bundle: DatasetBundle,
    output_dir: Path,
    seed: int,
    max_pairs: int,
) -> dict[str, Any]:
    """Train on train entities, lock thresholds on validation, and score test."""
    splits = split_by_entity(bundle, seed)
    validation = splits["validation"]
    test = splits["test"]

    baseline_val_raw, baseline_val_seconds, baseline_val_rss = _run_baseline(validation)
    baseline_test_raw, baseline_test_seconds, baseline_test_rss = _run_baseline(test)
    _baseline_val, baseline_val_removed = _filter_safe(baseline_val_raw, validation)
    baseline_test, baseline_test_removed = _filter_safe(baseline_test_raw, test)
    baseline_test_metrics = _evaluate_test(
        test, baseline_test, baseline_test_removed, FUZZY_REVIEW_THRESHOLD, stable_order=True
    )
    baseline_test_metrics["threshold"] = FUZZY_REVIEW_THRESHOLD

    model_path = output_dir / "models" / f"{bundle.name}-settings.json"
    train_seconds, train_rss = _train_model(
        splits["train"], bundle.profile, model_path, max_pairs, seed
    )

    rerank_val_raw, rerank_val_seconds, rerank_val_rss = _score_with_splink(
        validation, bundle.profile, model_path, baseline_val_raw
    )
    rerank_val, rerank_val_removed = _filter_safe(rerank_val_raw, validation)
    rerank_threshold = _choose_threshold(
        rerank_val, validation.truth_pairs, REVIEW_BUDGET_PER_RECORD
    )
    rerank_test_raw, rerank_test_seconds, rerank_test_rss = _score_with_splink(
        test, bundle.profile, model_path, baseline_test_raw
    )
    rerank_test, rerank_test_removed = _filter_safe(rerank_test_raw, test)
    rerank_metrics = _evaluate_test(test, rerank_test, rerank_test_removed, rerank_threshold)

    blocked_val_raw, blocked_val_seconds, blocked_val_rss = _score_with_splink(
        validation, bundle.profile, model_path
    )
    blocked_val, blocked_val_removed = _filter_safe(blocked_val_raw, validation)
    blocked_threshold = _choose_threshold(
        blocked_val, validation.truth_pairs, REVIEW_BUDGET_PER_RECORD
    )
    blocked_test_raw, blocked_test_seconds, blocked_test_rss = _score_with_splink(
        test, bundle.profile, model_path
    )
    blocked_test, blocked_test_removed = _filter_safe(blocked_test_raw, test)
    blocked_metrics = _evaluate_test(test, blocked_test, blocked_test_removed, blocked_threshold)

    split_entities = {
        name: len({split.truth_by_key[key] for key in split.truth_by_key})
        for name, split in splits.items()
    }
    trusted_id_coverage = {
        name: round(
            sum(value is not None for value in split.trusted_id_by_key.values())
            / max(1, len(split.trusted_id_by_key)),
            6,
        )
        for name, split in splits.items()
    }
    return {
        "split_entities": split_entities,
        "trusted_identifier_coverage_not_used_by_splink": trusted_id_coverage,
        "cannot_link_pairs_in_test": len(test.cannot_link_pairs),
        "scoring_columns": list(bundle.feature_columns),
        "blocking_only_columns": list(bundle.blocking_only_columns),
        "all_non_null_comparison_m_and_u_estimates_present": True,
        "excluded_from_splink_scoring": [
            "ground_truth_entity_id",
            "trusted_external_identifier",
            "record_key",
            "entity_type_safety_gate",
        ],
        "methods": {
            "current_resolver": {
                "candidate_policy": (
                    "existing exact trusted-ID/alias lookup and fuzzy overlap cutoff; "
                    "sorted candidate IDs"
                ),
                "threshold": FUZZY_REVIEW_THRESHOLD,
                "validation_runtime_seconds": round(baseline_val_seconds, 6),
                "test_runtime_seconds": round(baseline_test_seconds, 6),
                "peak_rss_bytes": max(baseline_val_rss, baseline_test_rss),
                "test": baseline_test_metrics,
            },
            "splink_rerank_current_candidates": {
                "threshold_locked_on_validation": rerank_threshold,
                "training_runtime_seconds": round(train_seconds, 6),
                "validation_runtime_seconds": round(rerank_val_seconds, 6),
                "test_runtime_seconds": round(rerank_test_seconds, 6),
                "peak_rss_bytes": max(train_rss, rerank_val_rss, rerank_test_rss),
                "validation_unsafe_candidates_removed": rerank_val_removed,
                "test": rerank_metrics,
            },
            "splink_independent_blocking": {
                "threshold_locked_on_validation": blocked_threshold,
                "training_runtime_seconds": round(train_seconds, 6),
                "validation_runtime_seconds": round(blocked_val_seconds, 6),
                "test_runtime_seconds": round(blocked_test_seconds, 6),
                "peak_rss_bytes": max(train_rss, blocked_val_rss, blocked_test_rss),
                "validation_unsafe_candidates_removed": blocked_val_removed,
                "test": blocked_metrics,
            },
            "safety_filter_counts": {
                "baseline_validation_candidates_removed": baseline_val_removed,
                "baseline_test_candidates_removed": baseline_test_removed,
                "rerank_validation_candidates_removed": rerank_val_removed,
                "rerank_test_candidates_removed": rerank_test_removed,
                "independent_validation_candidates_removed": blocked_val_removed,
                "independent_test_candidates_removed": blocked_test_removed,
            },
        },
    }


def _versions() -> dict[str, str]:
    """Return exact versions of the isolated benchmark libraries."""
    import duckdb
    import splink

    return {"splink": splink.__version__, "duckdb": duckdb.__version__}


def run_evaluation(
    data_dir: Path,
    output_path: Path,
    *,
    seed: int = SEED,
    synthetic_entities: int = 900,
    max_pairs: int = 100_000,
) -> dict[str, Any]:
    """Run both offline datasets and write a summary-only report."""
    _require_febrl4(data_dir)
    output_dir = output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    bundles = (
        _synthetic_dataset(synthetic_entities, seed),
        _febrl4_dataset(data_dir),
    )
    results = {
        bundle.name: _evaluate_dataset(bundle, output_dir, seed, max_pairs) for bundle in bundles
    }
    import psutil

    report = {
        "protocol": {
            "seed": seed,
            "entity_split": {"train": 0.60, "validation": 0.20, "test": 0.20},
            "review_budget_per_source_record": REVIEW_BUDGET_PER_RECORD,
            "threshold_selection": "maximize pairwise F1 on validation, then lock before test",
            "splink_training": "unsupervised u sampling and EM on train split only",
            "splink_writes_identity_store_or_event_log": False,
            "production_resolver_store_used": False,
        },
        "environment": {
            "python": platform.python_version(),
            **_versions(),
            "psutil": psutil.__version__,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "logical_cpu_count": psutil.cpu_count(logical=True),
            "physical_memory_bytes": psutil.virtual_memory().total,
        },
        "datasets": {
            "synthetic": {
                "entities": synthetic_entities,
                "generator": (
                    "fixed multilingual aliases, typos, duplicate names, missing trusted IDs, "
                    "types, and cannot-link pairs"
                ),
            },
            "febrl4": {
                "entities": 5_000,
                "rows_per_side": 5_000,
                "source_revision": _load_manifest()["source_revision"],
                "files": {
                    filename: {"sha256": entry["sha256"]}
                    for filename, entry in _load_manifest()["files"].items()
                },
                "ground_truth": (
                    "parsed from source rec_id solely for splitting/scoring; not passed to Splink"
                ),
                "trusted_identifier": (
                    "soc_sec_id available only to the current resolver baseline; "
                    "excluded from Splink columns"
                ),
            },
        },
        "results": results,
    }
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--synthetic-entities", type=int, default=900)
    parser.add_argument("--max-pairs", type=int, default=100_000)
    parser.add_argument("--fetch-data", action="store_true", help="download and checksum FEBRL4")
    args = parser.parse_args()
    if args.fetch_data:
        for filename, digest in fetch_febrl4(args.data_dir).items():
            print(f"verified {filename} sha256={digest}")
        return 0
    report = run_evaluation(
        args.data_dir,
        args.output,
        seed=args.seed,
        synthetic_entities=args.synthetic_entities,
        max_pairs=args.max_pairs,
    )
    print(f"wrote {args.output}")
    for dataset, result in report["results"].items():
        for method, values in result["methods"].items():
            if method == "safety_filter_counts":
                continue
            metrics = values["test"]
            print(
                f"{dataset} {method}: candidate_recall={metrics['candidate_recall']:.3f} "
                f"recall@5={metrics['recall_at_5']:.3f} "
                f"precision={metrics['pairwise_precision']:.3f} "
                f"recall={metrics['pairwise_recall']:.3f} "
                f"false={metrics['false_suggestions']} "
                f"unsafe={metrics['unsafe_suggestions']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
