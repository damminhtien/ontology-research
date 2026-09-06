"""Data-contract versions, loaded from the repo ``VERSION`` file.

Version numbers are **data, not code**: the current values live in the root
``VERSION`` file and their history in ``CHANGELOG-DATA.md``. Source modules
import the constants below instead of hardcoding numbers, so bumping a data
contract never means hunting for literals across the codebase — it is one edit
to ``VERSION`` plus a changelog entry (and, for the event schema, a registered
upcaster, per ADR-0002/0009).

Reading is cached per process: changing ``VERSION`` mid-run is not a supported
operation (contract changes are deliberate, versioned releases).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"

#: Keys this deployment defines. Unknown keys in the file are rejected so a
#: typo cannot silently create a contract nobody reads.
KNOWN_CONTRACTS = ("event_schema", "lake")


@lru_cache(maxsize=1)
def _repo_versions() -> dict[str, int]:
    return load_versions(_VERSION_FILE)


def load_versions(path: Path) -> dict[str, int]:
    """Parse a ``key = integer`` VERSION file into a dict.

    Raises:
        ValueError: On a malformed line, a non-integer value, a non-positive
            version, an unknown contract key, or a missing file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read version file {path}: {exc}") from exc

    versions: dict[str, int] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(f"{path}:{lineno}: expected 'key = integer', got {raw!r}")
        key = key.strip()
        if key not in KNOWN_CONTRACTS:
            expected = ", ".join(KNOWN_CONTRACTS)
            raise ValueError(
                f"{path}:{lineno}: unknown contract {key!r}; expected one of {expected}"
            )
        try:
            number = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: {key} must be an integer, got {value!r}") from exc
        if number < 1:
            raise ValueError(f"{path}:{lineno}: {key} must be >= 1, got {number}")
        versions[key] = number

    missing = [key for key in KNOWN_CONTRACTS if key not in versions]
    if missing:
        raise ValueError(f"{path}: missing contract version(s): {missing}")
    return versions


def contract_version(key: str) -> int:
    """Current version of the named data contract.

    Raises:
        ValueError: If the VERSION file is malformed or the key is unknown.
    """
    return _repo_versions()[key]


#: Event payload contract version (see ``foundry.events`` and ADR-0009).
EVENT_SCHEMA_VERSION = contract_version("event_schema")

#: Lake layout version written into ``manifest.json`` (see ``foundry.lake``).
LAKE_VERSION = contract_version("lake")
