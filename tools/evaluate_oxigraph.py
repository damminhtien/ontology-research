#!/usr/bin/env python3
"""Evaluate rebuildable Oxigraph snapshots against RDFLib and read-only Jena."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "tools", REPO_ROOT):
    if _path.as_posix() not in sys.path:
        sys.path.insert(0, _path.as_posix())

import cq_runner  # noqa: E402
from benchmark import generate_events  # noqa: E402
from check_conformance import (  # noqa: E402
    JENA_SHA512,
    JENA_URL,
    _download,
    _java_executable,
    _jena_query,
)
from rdflib import Graph, Literal, URIRef  # noqa: E402
from rdflib.namespace import XSD  # noqa: E402

from foundry.events import EventLog  # noqa: E402
from foundry.namespaces import CORE_LOCATED_AT, CORE_ONTOLOGY_NS  # noqa: E402
from foundry.oxigraph_snapshot import (  # noqa: E402
    build_snapshot,
    current_snapshot,
    directory_size_bytes,
    open_current_store,
)

ASSERTION_NS = "https://damminhtien.github.io/ontology-research/ontology/middle/assertion#"
CORE_NS = CORE_ONTOLOGY_NS
JENA_VERSION = "6.2.0"
OXIGRAPH_VERSION = "0.5.13"
FUSEKI_URL = "https://downloads.apache.org/jena/binaries/apache-jena-fuseki-6.2.0.tar.gz"
FUSEKI_SHA512 = (
    "ba65f5867d2d4741b2ed9e2af5a0d4fbb447909894ab2a0c6bc4dac8997f4fe3"
    "39c87b13c48d45d054977769f0f8bf763ea346b1f7792d5cdc458041bd43a132"
)
QUERIES_PER_ENTITY = 10


def _percentile(values: list[float], fraction: float) -> float:
    """Return the nearest-rank percentile of a non-empty sample."""
    if not values:
        raise ValueError("cannot compute a percentile from no samples")
    return sorted(values)[max(0, math.ceil(fraction * len(values)) - 1)]


def _query_statistics(values: list[float], *, warmups: int) -> dict[str, float | int]:
    """Render per-call millisecond percentiles and sampling metadata."""
    return {
        "sample_count": len(values),
        "warmup_count": warmups,
        "p50_ms": round(_percentile(values, 0.50), 4),
        "p95_ms": round(_percentile(values, 0.95), 4),
        "p99_ms": round(_percentile(values, 0.99), 4),
    }


def _normalize_value(value: Any) -> object:
    """Normalize PyOxigraph and SPARQL JSON terms to CQ-compatible cells."""
    if value is None:
        return None
    if hasattr(value, "value"):
        datatype_term = getattr(value, "datatype", None)
        datatype = getattr(datatype_term, "value", str(datatype_term or ""))
        lexical = str(value.value)
        if datatype == str(XSD.boolean):
            return lexical in {"true", "1"}
        if datatype in {
            str(XSD.integer),
            str(XSD.int),
            str(XSD.long),
            str(XSD.short),
            str(XSD.byte),
            str(XSD.nonNegativeInteger),
            str(XSD.positiveInteger),
            str(XSD.nonPositiveInteger),
            str(XSD.negativeInteger),
            str(XSD.unsignedInt),
            str(XSD.unsignedLong),
            str(XSD.unsignedShort),
            str(XSD.unsignedByte),
        }:
            return int(lexical)
        if datatype == str(XSD.dateTime) and lexical.endswith("+00:00"):
            return lexical[:-6] + "Z"
        return lexical
    if isinstance(value, dict):
        kind = value.get("type")
        lexical = value.get("value")
        if kind in {"uri", "bnode"}:
            return lexical
        datatype = value.get("datatype", "")
        if datatype == str(XSD.boolean):
            return lexical in {"true", "1"}
        if datatype in {
            str(XSD.integer),
            str(XSD.int),
            str(XSD.long),
            str(XSD.short),
            str(XSD.byte),
            str(XSD.nonNegativeInteger),
            str(XSD.positiveInteger),
            str(XSD.nonPositiveInteger),
            str(XSD.negativeInteger),
            str(XSD.unsignedInt),
            str(XSD.unsignedLong),
            str(XSD.unsignedShort),
            str(XSD.unsignedByte),
        }:
            return int(lexical)
        if datatype == str(XSD.dateTime) and lexical.endswith("+00:00"):
            return lexical[:-6] + "Z"
        return lexical
    return str(value)


def _oxigraph_rows(store, query: str) -> tuple[list[str], list[list[object]]]:
    """Run one SELECT query and normalize its bindings."""
    result = store.query(query)
    variables = [variable.value for variable in result.variables]
    rows = [
        [_normalize_value(solution[variable]) for variable in result.variables]
        for solution in result
    ]
    return variables, rows


def _same_rows(actual: list[list[object]], expected: list[list[object]], ordered: bool) -> bool:
    """Compare ordered SELECT output or unordered multisets as SPARQL requires."""
    if ordered:
        return actual == expected
    return Counter(map(repr, actual)) == Counter(map(repr, expected))


def _values_query(query_text: str, spec: dict) -> str:
    """Insert typed VALUES bindings; preserve the q31 UNION scope workaround."""
    bindings = cq_runner._query_bindings(spec)
    if bindings:
        import re

        match = re.search(r"\bWHERE\s*\{", query_text, flags=re.IGNORECASE)
        if match is None:
            raise ValueError(f"query {spec['id']} has bindings but no WHERE group")
        variables = " ".join(f"?{name}" for name in bindings)
        terms = " ".join(term.n3() for term in bindings.values())
        query_text = (
            query_text[: match.end()]
            + f" VALUES ({variables}) {{ ({terms}) }} "
            + query_text[match.end() :]
        )
    if spec.get("query_file") == "q31_sensor_provenance.rq":
        query_text = query_text.replace("FILTER (?observation = ?wantedObservation)", "")
        query_text = query_text.replace("FILTER (?trackId = ?wantedTrackId)", "")
        query_text = query_text.replace(
            "  ?sensor core:name ?sensorName .",
            "  ?sensor core:name ?sensorName .\n"
            "  FILTER ((BOUND(?wantedObservation) && !BOUND(?trackId) && "
            "?observation = ?wantedObservation) || (BOUND(?wantedTrackId) && "
            "BOUND(?trackId) && ?trackId = ?wantedTrackId))",
        )
    return query_text


def _prepare_seed_graph(path: Path) -> Graph:
    """Serialize the registered seed graph once for all three query engines."""
    graph = cq_runner.load_benchmark_graph()
    path.write_text(graph.serialize(format="nt"), encoding="utf-8")
    return graph


def _run_seed_cqs(tmp: Path, jena_home: Path, java_home: Path) -> dict[str, int]:
    """Require all 41 seed CQs to match ground truth in RDFLib, Oxigraph, and ARQ."""
    from pyoxigraph import RdfFormat, Store

    data_path = tmp / "competency-seed.nt"
    rdflib_graph = _prepare_seed_graph(data_path)
    oxigraph = Store()
    oxigraph.load(path=str(data_path), format=RdfFormat.N_TRIPLES)
    arq = jena_home / "bin" / "arq"
    env = dict(os.environ, JAVA_HOME=str(java_home), JENA_HOME=str(jena_home))
    specs = sorted(cq_runner.EXPECTED_DIR.glob("*.json"))
    if len(specs) != 41:
        raise RuntimeError(f"expected 41 competency queries, found {len(specs)}")

    for index, spec_path in enumerate(specs):
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        query_path = cq_runner.QUERIES_DIR / spec["query_file"]
        query_text = query_path.read_text(encoding="utf-8")
        ordered = "ORDER BY" in query_text.upper()

        rdflib_result = cq_runner.run_query_spec(rdflib_graph, spec)
        if not rdflib_result["passed"]:
            raise RuntimeError(f"RDFLib {spec['id']} failed: {rdflib_result['error']}")
        expected = [
            [cell if cell is None or isinstance(cell, (bool, int)) else str(cell) for cell in row]
            for row in spec["rows"]
        ]

        prepared = _values_query(query_text, spec)
        ox_variables, ox_rows = _oxigraph_rows(oxigraph, prepared)
        if ox_variables != spec["variables"] or not _same_rows(ox_rows, expected, ordered):
            raise RuntimeError(
                f"Oxigraph {spec['id']} differs from expected output: "
                f"variables={ox_variables}, rows={ox_rows}"
            )

        # _jena_query writes one query file per call; keep each call in a temp directory.
        query_tmp = tmp / f"jena-cq-{index}"
        query_tmp.mkdir()
        jena_variables, jena_rows = _jena_query(
            query_path, query_text, spec, data_path, arq, query_tmp, env
        )
        if jena_variables != spec["variables"] or not _same_rows(jena_rows, expected, ordered):
            raise RuntimeError(
                f"Jena ARQ {spec['id']} differs from expected output: "
                f"variables={jena_variables}, rows={jena_rows}"
            )
    del oxigraph
    return {
        "total": len(specs),
        "rdflib": len(specs),
        "oxigraph": len(specs),
        "jena_arq": len(specs),
    }


def _reserve_port() -> int:
    """Return an available loopback TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _jena_archives(cache_dir: Path) -> tuple[Path, Path, Path, Path]:
    """Fetch and verify matching Apache Jena and Fuseki 6.2.0 distributions."""
    cache_dir.mkdir(parents=True, exist_ok=True)

    def verified_archive(name: str, url: str, digest: str) -> Path:
        archive = cache_dir / name
        if archive.is_file():
            hasher = hashlib.sha512()
            with archive.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    hasher.update(chunk)
            if hasher.hexdigest() != digest:
                archive.unlink()
        if not archive.is_file():
            _download(url, archive, digest)
        return archive

    archive = verified_archive(f"apache-jena-{JENA_VERSION}.tar.gz", JENA_URL, JENA_SHA512)
    fuseki_archive = verified_archive(
        f"apache-jena-fuseki-{JENA_VERSION}.tar.gz", FUSEKI_URL, FUSEKI_SHA512
    )
    jena_home = cache_dir / f"apache-jena-{JENA_VERSION}"
    if not (jena_home / "bin" / "tdb2.tdbloader").is_file():
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(cache_dir, filter="data")
    fuseki_home = cache_dir / f"apache-jena-fuseki-{JENA_VERSION}"
    if not (fuseki_home / "fuseki-server").is_file():
        with tarfile.open(fuseki_archive, "r:gz") as bundle:
            bundle.extractall(cache_dir, filter="data")
    if not (fuseki_home / "fuseki-server").is_file():
        raise RuntimeError(f"Fuseki {JENA_VERSION} archive has no server script")
    return archive, jena_home, fuseki_archive, fuseki_home


def _write_fuseki_config(path: Path, dataset_path: Path) -> None:
    """Configure a query-only Fuseki service backed by one TDB2 directory."""
    dataset_literal = json.dumps(dataset_path.resolve().as_posix())
    path.write_text(
        "@prefix fuseki: <http://jena.apache.org/fuseki#> .\n"
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
        "@prefix tdb2: <http://jena.apache.org/2016/tdb#> .\n"
        "[] rdf:type fuseki:Server ;\n"
        "   fuseki:services ( <#service> ) .\n"
        "<#service> rdf:type fuseki:Service ;\n"
        '   fuseki:name "ds" ;\n'
        "   fuseki:dataset <#dataset> ;\n"
        '   fuseki:endpoint [ fuseki:operation fuseki:query ; fuseki:name "query" ] .\n'
        "<#dataset> rdf:type tdb2:DatasetTDB2 ;\n"
        f"   tdb2:location {dataset_literal} .\n",
        encoding="utf-8",
    )


class _PeakRssSampler:
    """Sample one process's resident set size while a benchmark phase runs."""

    def __init__(self, pid: int, interval: float = 0.1) -> None:
        self.pid = pid
        self.interval = interval
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _read_rss(self) -> int:
        """Read resident bytes for the sampled process."""
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(self.pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            return int(result.stdout.strip()) * 1024
        except ValueError:
            return 0

    def _sample(self) -> None:
        while not self._stop.is_set():
            rss_bytes = self._read_rss()
            if rss_bytes == 0:
                break
            self.peak_bytes = max(self.peak_bytes, rss_bytes)
            self._stop.wait(self.interval)

    def __enter__(self) -> _PeakRssSampler:
        self.peak_bytes = self._read_rss()
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


class _ReadOnlyFuseki:
    """Start a query-only Fuseki process for a TDB2 dataset on loopback."""

    def __init__(self, fuseki_home: Path, dataset_path: Path, work_dir: Path, java_home: Path):
        self.fuseki_home = fuseki_home
        self.dataset_path = dataset_path
        self.work_dir = work_dir
        self.java_home = java_home
        self.port = _reserve_port()
        self.process: subprocess.Popen | None = None

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/ds/query"

    def __enter__(self) -> _ReadOnlyFuseki:
        config = self.work_dir / "fuseki.ttl"
        _write_fuseki_config(config, self.dataset_path)
        env = dict(os.environ, JAVA_HOME=str(self.java_home), FUSEKI_HOME=str(self.fuseki_home))
        with (self.work_dir / "fuseki.log").open("w", encoding="utf-8") as output:
            self.process = subprocess.Popen(
                [
                    str(self.fuseki_home / "fuseki-server"),
                    "--config",
                    str(config),
                    "--localhost",
                    "--port",
                    str(self.port),
                ],
                cwd=self.work_dir,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        "Fuseki exited during startup:\n"
                        + (self.work_dir / "fuseki.log").read_text(encoding="utf-8")
                    )
                try:
                    _post_query(self.endpoint, "ASK {}")
                    return self
                except (OSError, urllib.error.URLError, RuntimeError):
                    time.sleep(0.2)
            raise TimeoutError("Fuseki did not become ready on loopback within 45 seconds")
        except Exception:
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=10)
            raise

    def __exit__(self, *_exc) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def _post_query(endpoint: str, query: str) -> dict:
    """Issue one SPARQL query over HTTP and return the decoded result document."""
    request = urllib.request.Request(
        endpoint,
        data=urllib.parse.urlencode({"query": query}).encode("utf-8"),
        headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Fuseki HTTP {exc.code}: {body}") from exc
    return result


def _http_rows(endpoint: str, query: str) -> tuple[list[str], list[list[object]]]:
    """Run a SELECT query over Fuseki and normalize SPARQL JSON bindings."""
    result = _post_query(endpoint, query)
    variables = result["head"]["vars"]
    rows = [
        [_normalize_value(binding.get(variable)) for variable in variables]
        for binding in result["results"]["bindings"]
    ]
    return variables, rows


def _query_set(entity_iri: str) -> dict[str, str]:
    """Create the fixed point, temporal, and provenance queries for one entity."""
    values = URIRef(entity_iri).n3()
    instant = Literal("2026-08-15T12:00:00Z", datatype=XSD.dateTime).n3()
    return {
        "point_lookup": (
            f"SELECT ?name WHERE {{ VALUES ?entity {{ {values} }} ?entity <{CORE_NS}name> ?name }}"
        ),
        "temporal": (
            "SELECT ?location WHERE { VALUES (?entity ?instant) { ("
            f"{values} {instant}) }} "
            "?assertion <"
            f"{ASSERTION_NS}predicate> <{CORE_LOCATED_AT}> ; "
            f"<{CORE_NS}describes> ?entity ; <{ASSERTION_NS}hasObject> ?location ; "
            f"<{CORE_NS}validFrom> ?validFrom . "
            f"OPTIONAL {{ ?assertion <{CORE_NS}validUntil> ?validUntil }} "
            "FILTER (?validFrom <= ?instant && (!BOUND(?validUntil) || ?instant < ?validUntil)) }"
        ),
        "provenance": (
            "SELECT ?assertion ?source WHERE { VALUES ?entity { "
            f"{values} }} ?assertion <{CORE_NS}describes> ?entity ; "
            f"<{CORE_NS}hasSource> ?source }}"
        ),
    }


def _validate_workload_rows(name: str, variables: list[str], rows: list[list[object]]) -> None:
    """Require the generated benchmark data to exercise every workload query."""
    expected_counts = {"point_lookup": 1, "temporal": 1, "provenance": QUERIES_PER_ENTITY}
    expected_variables = {
        "point_lookup": ["name"],
        "temporal": ["location"],
        "provenance": ["assertion", "source"],
    }
    if variables != expected_variables[name] or len(rows) != expected_counts[name]:
        raise RuntimeError(
            f"{name} workload returned {variables} and {len(rows)} rows; "
            f"expected {expected_variables[name]} and {expected_counts[name]}"
        )


def _profile_query_memory(store, endpoint: str, *, entities: int, jena_pid: int) -> tuple[int, int]:
    """Sample engine RSS during untimed representative queries."""
    indices = sorted({0, entities // 4, entities // 2, (3 * entities) // 4, entities - 1})
    with (
        _PeakRssSampler(os.getpid()) as oxigraph_rss,
        _PeakRssSampler(jena_pid) as jena_rss,
    ):
        for entity_index in indices:
            entity_iri = f"urn:world:entity:bench-{entity_index:08d}"
            for name, query in _query_set(entity_iri).items():
                variables, rows = _oxigraph_rows(store, query)
                _validate_workload_rows(name, variables, rows)
                http_variables, http_rows = _http_rows(endpoint, query)
                if variables != http_variables or not _same_rows(rows, http_rows, ordered=False):
                    raise RuntimeError(
                        f"Oxigraph and Jena returned different {name} rows at entity {entity_index}"
                    )
    return oxigraph_rss.peak_bytes, jena_rss.peak_bytes


def _check_read_only(endpoint: str) -> None:
    """Require Fuseki to reject updates while permitting SPARQL queries."""
    update_url = endpoint.rsplit("/", 1)[0] + "/update"
    request = urllib.request.Request(
        update_url,
        data=b"DELETE WHERE { <urn:ontology-research:unused-probe> ?p ?o }",
        headers={"Content-Type": "application/sparql-update"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raise RuntimeError(f"Fuseki exposed an update endpoint (HTTP {response.status})")
    except urllib.error.HTTPError as exc:
        if exc.code not in {404, 405}:
            raise RuntimeError(f"unexpected update endpoint response: HTTP {exc.code}") from exc


def _measure_queries(store, endpoint: str, *, entities: int, runs: int) -> dict:
    """Measure local Oxigraph calls and separately labelled Fuseki HTTP calls."""
    indices = sorted({min(entities - 1, index * entities // runs) for index in range(runs)})
    names = ("point_lookup", "temporal", "provenance")
    oxigraph_samples = {name: [] for name in names}
    fuseki_samples = {name: [] for name in names}
    warmup_count = min(5, len(indices))

    for entity_index in indices[:warmup_count]:
        entity_iri = f"urn:world:entity:bench-{entity_index:08d}"
        for name, query in _query_set(entity_iri).items():
            ox_variables, ox_rows = _oxigraph_rows(store, query)
            http_variables, http_rows = _http_rows(endpoint, query)
            _validate_workload_rows(name, ox_variables, ox_rows)
            if ox_variables != http_variables or not _same_rows(ox_rows, http_rows, ordered=False):
                raise RuntimeError(
                    f"Oxigraph and Jena returned different {name} rows: "
                    f"oxigraph={ox_rows}, jena={http_rows}"
                )

    for sample_index, entity_index in enumerate(indices):
        entity_iri = f"urn:world:entity:bench-{entity_index:08d}"
        for name, query in _query_set(entity_iri).items():
            started = time.perf_counter_ns()
            variables, rows = _oxigraph_rows(store, query)
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            _validate_workload_rows(name, variables, rows)
            oxigraph_samples[name].append(elapsed)

            started = time.perf_counter_ns()
            http_variables, http_rows = _http_rows(endpoint, query)
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            if variables != http_variables or not _same_rows(rows, http_rows, ordered=False):
                raise RuntimeError(
                    f"Oxigraph and Jena differ for {name} at sample {sample_index}: "
                    f"oxigraph={rows}, jena={http_rows}"
                )
            fuseki_samples[name].append(elapsed)
    return {
        "samples_per_query": len(indices),
        "warmup_count": warmup_count,
        "oxigraph_direct_engine": {
            name: _query_statistics(values, warmups=warmup_count)
            for name, values in oxigraph_samples.items()
        },
        "jena_fuseki_loopback_http_engine_plus_http": {
            name: _query_statistics(values, warmups=warmup_count)
            for name, values in fuseki_samples.items()
        },
        "peak_rss_bytes": {},
    }


def _java_version(java_home: Path) -> str:
    """Return the full Java runtime version string used for the benchmark."""
    result = subprocess.run(
        [str(java_home / "bin" / "java"), "-version"],
        capture_output=True,
        text=True,
        check=True,
    )
    return (result.stdout + result.stderr).splitlines()[0].strip()


def _run_monitored_process(
    command: list[str], *, cwd: Path, env: dict[str, str], log_path: Path
) -> tuple[float, int, str]:
    """Run a child process, recording its peak resident memory and log output."""
    started = time.perf_counter()
    with log_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        with _PeakRssSampler(process.pid) as sampler:
            return_code = process.wait()
    elapsed = time.perf_counter() - started
    output_text = log_path.read_text(encoding="utf-8")
    if return_code:
        raise RuntimeError(f"command failed ({return_code}): {command}\n{output_text}")
    return elapsed, sampler.peak_bytes, output_text


def _hardware_metadata() -> dict[str, object]:
    """Record stable local hardware and operating-system descriptors."""
    memory_bytes = None
    with contextlib.suppress(AttributeError, OSError, ValueError):
        memory_bytes = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "memory_bytes": memory_bytes,
    }


def _build_scale(
    *,
    scale: int,
    runs: int,
    out_dir: Path,
    jena_home: Path,
    fuseki_home: Path,
    java_home: Path,
    temporary_root: Path,
) -> dict:
    """Generate one scale, build/reopen/rebuild snapshots, and benchmark both stores."""
    from pyoxigraph import RdfFormat

    scale_dir = out_dir / f"scale-{scale}"
    scale_dir.mkdir(parents=True, exist_ok=True)
    event_log_path = scale_dir / "benchmark-events.jsonl"
    for segment in EventLog(event_log_path)._segments():
        segment.unlink(missing_ok=True)
    EventLog(event_log_path).extend(generate_events(scale, QUERIES_PER_ENTITY, seed=42))
    snapshot_root = scale_dir / "snapshots"

    with _PeakRssSampler(os.getpid()) as first_build_rss:
        first_started = time.perf_counter()
        first_snapshot = build_snapshot(event_log_path, snapshot_root, profile="canonical-v1")
    first_build_seconds = time.perf_counter() - first_started
    comparison_queries = _query_set("urn:world:entity:bench-00000000")
    with _PeakRssSampler(os.getpid()) as first_reopen_rss:
        first_store = open_current_store(snapshot_root)
        first_probe = {
            name: _oxigraph_rows(first_store, query) for name, query in comparison_queries.items()
        }
    del first_store

    with _PeakRssSampler(os.getpid()) as rebuild_rss:
        rebuild_started = time.perf_counter()
        snapshot_path = build_snapshot(event_log_path, snapshot_root, profile="canonical-v1")
    rebuild_seconds = time.perf_counter() - rebuild_started
    if current_snapshot(snapshot_root) != snapshot_path or first_snapshot == snapshot_path:
        raise RuntimeError("full rebuild did not atomically publish a new snapshot")

    oxigraph_started = time.perf_counter()
    store = open_current_store(snapshot_root)
    reopen_seconds = time.perf_counter() - oxigraph_started
    second_probe = {
        name: _oxigraph_rows(store, query) for name, query in comparison_queries.items()
    }
    if first_probe != second_probe:
        raise RuntimeError("query output changed after the full snapshot rebuild")

    projection_path = temporary_root / f"projection-{scale}.nq"
    store.dump(output=str(projection_path), format=RdfFormat.N_QUADS)
    jena_store_path = scale_dir / "jena-tdb2"
    if jena_store_path.exists():
        shutil.rmtree(jena_store_path)
    jena_store_path.mkdir()
    jena_load_seconds, jena_loader_peak_rss, _loader_output = _run_monitored_process(
        [
            str(java_home / "bin" / "java"),
            f"-Dlog4j.configurationFile=file:{jena_home / 'log4j2.properties'}",
            "-cp",
            f"{jena_home}/lib/*",
            "tdb2.tdbloader",
            "--loc",
            str(jena_store_path),
            str(projection_path),
        ],
        cwd=scale_dir,
        env=dict(os.environ, JAVA_HOME=str(java_home), JENA_HOME=str(jena_home)),
        log_path=scale_dir / "tdb2-loader.log",
    )
    projection_path.unlink(missing_ok=True)
    tdb_bytes = directory_size_bytes(jena_store_path)
    manifest = json.loads((snapshot_path / "manifest.json").read_text(encoding="utf-8"))
    snapshot_bytes = directory_size_bytes(snapshot_path / "store")

    with _ReadOnlyFuseki(fuseki_home, jena_store_path, scale_dir, java_home) as fuseki:
        _check_read_only(fuseki.endpoint)
        oxigraph_runtime_rss, jena_runtime_rss = _profile_query_memory(
            store, fuseki.endpoint, entities=scale, jena_pid=fuseki.process.pid
        )
        query_metrics = _measure_queries(store, fuseki.endpoint, entities=scale, runs=runs)
    query_metrics["peak_rss_bytes"].update(
        {
            "oxigraph_first_build_process": first_build_rss.peak_bytes,
            "oxigraph_first_reopen_process": first_reopen_rss.peak_bytes,
            "oxigraph_rebuild_process": rebuild_rss.peak_bytes,
            "oxigraph_runtime_process": oxigraph_runtime_rss,
            "jena_tdb2_loader_process": jena_loader_peak_rss,
            "jena_fuseki_server_process": jena_runtime_rss,
        }
    )
    del store

    return {
        "entities": scale,
        "assertions_per_entity": QUERIES_PER_ENTITY,
        "seed": 42,
        "events": manifest["source"]["event_count"],
        "quads": manifest["projection"]["emitted_quad_count"],
        "triples": manifest["projection"]["triple_count"],
        "snapshot": {
            "first_build_seconds": round(first_build_seconds, 3),
            "rebuild_seconds": round(rebuild_seconds, 3),
            "reopen_seconds": round(reopen_seconds, 6),
            "store_bytes": snapshot_bytes,
            "manifest": manifest,
            "old_and_new_snapshot_query_equal": True,
        },
        "jena_tdb2": {
            "load_seconds": round(jena_load_seconds, 3),
            "store_bytes": tdb_bytes,
            "query_endpoint": "loopback only; query-only Fuseki config; update endpoint rejected",
        },
        "query_latency": query_metrics,
    }


def _parse_scales(value: str) -> list[int]:
    """Parse the plan's required 10K and 100K entity scales."""
    try:
        scales = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "scales must be comma-separated positive integers"
        ) from exc
    if not scales or any(scale < 1 for scale in scales):
        raise argparse.ArgumentTypeError("scales must be comma-separated positive integers")
    return scales


def main() -> int:
    """Run seed equivalence checks and the local 10K/100K store benchmark."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scales", type=_parse_scales, default=[10_000, 100_000])
    parser.add_argument("--runs", type=int, default=100, help="per-query latency samples per scale")
    parser.add_argument("--out", type=Path, default=Path("build/oxigraph-evaluation"))
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    java_home, _java = _java_executable()
    _jena_archive, jena_home, _fuseki_archive, fuseki_home = _jena_archives(out_dir / "tool-cache")

    with tempfile.TemporaryDirectory(prefix="ontology-oxigraph-") as tmp_name:
        temporary_root = Path(tmp_name)
        seed_cqs = _run_seed_cqs(temporary_root, jena_home, java_home)
        scale_results = [
            _build_scale(
                scale=scale,
                runs=args.runs,
                out_dir=out_dir,
                jena_home=jena_home,
                fuseki_home=fuseki_home,
                java_home=java_home,
                temporary_root=temporary_root,
            )
            for scale in args.scales
        ]

    report = {
        "report_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "software": {
            "python": sys.version.split()[0],
            "pyoxigraph": OXIGRAPH_VERSION,
            "apache_jena": JENA_VERSION,
            "java": _java_version(java_home),
        },
        "hardware": _hardware_metadata(),
        "seed_cq_equivalence": seed_cqs,
        "scales": scale_results,
        "measurement_notes": {
            "oxigraph": "direct in-process Store.query calls",
            "jena": "Fuseki SPARQL HTTP on 127.0.0.1, so percentiles include HTTP overhead",
            "percentiles": (
                "per-call samples after warm-up; not comparable to historical batch averages"
            ),
            "snapshots": (
                "event-log full rebuild; previous CURRENT retained until completed store reopen"
            ),
        },
    }
    report_path = out_dir / "oxigraph-evaluation-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Seed CQs: RDFLib/Oxigraph/Jena ARQ {seed_cqs['total']}/41 each.")
    for result in scale_results:
        print(
            f"{result['entities']:,} entities: {result['triples']:,} triples; "
            f"Oxigraph build {result['snapshot']['first_build_seconds']:.2f}s, "
            f"TDB2 load {result['jena_tdb2']['load_seconds']:.2f}s."
        )
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Oxigraph evaluation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
