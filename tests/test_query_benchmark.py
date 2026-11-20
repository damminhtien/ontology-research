"""Checks for the shared PostgreSQL and Oxigraph query boundary."""

from __future__ import annotations

import time

import pytest

from foundry.query_benchmark import OxigraphQueryBackend
from foundry.query_service import QueryTimeoutError


class _SlowLazyStore:
    def query(self, _query: str):
        """Return a lazy result that exceeds a short wall-clock budget."""

        def rows():
            time.sleep(0.02)
            yield "row"

        return rows()


def test_oxigraph_timeout_includes_lazy_result_consumption() -> None:
    backend = OxigraphQueryBackend(_SlowLazyStore())

    with pytest.raises(QueryTimeoutError, match="exceeded 1 ms"):
        backend._query("SELECT * WHERE {}", timeout_ms=1)
