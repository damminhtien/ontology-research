"""Persistent local-console credentials and browser-session checks."""

from __future__ import annotations

import ipaddress
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from foundry.paths import data_root

CONSOLE_TOKEN_ENV = "FOUNDRY_CONSOLE_TOKEN"
CONSOLE_SESSION_COOKIE = "foundry_console_session"
CONSOLE_TOKEN_FILE = "console-token"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def console_token_path() -> Path:
    """Return the private persistent token path outside the repository."""
    return data_root() / "secrets" / CONSOLE_TOKEN_FILE


def ensure_local_console_token() -> str:
    """Set a stable console token, creating one in the private data root if needed."""
    configured = os.environ.get(CONSOLE_TOKEN_ENV, "").strip()
    if configured:
        os.environ[CONSOLE_TOKEN_ENV] = configured
        return configured

    path = console_token_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = _create_token_file(path)
    if not token:
        raise RuntimeError(f"local console token file is empty: {path}")

    path.chmod(0o600)
    os.environ[CONSOLE_TOKEN_ENV] = token
    return token


def _create_token_file(path: Path) -> str:
    """Atomically create a private token file, tolerating parallel startup."""
    token = secrets.token_urlsafe(48)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".console-token-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
            return token
        except FileExistsError:
            return path.read_text(encoding="utf-8").strip()
    finally:
        temporary_path.unlink(missing_ok=True)


def is_local_same_origin_request(request) -> bool:
    """Check that a browser request originates on the local console origin."""
    hostname = request.url.hostname
    if hostname not in LOCAL_HOSTS or request.client is None:
        return False
    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            return False
    except ValueError:
        return False

    origin = request.headers.get("origin")
    if not origin:
        return False
    parsed_origin = urlsplit(origin)
    try:
        origin_port = parsed_origin.port
    except ValueError:
        return False
    return (
        parsed_origin.scheme == request.url.scheme
        and parsed_origin.hostname == hostname
        and origin_port == request.url.port
        and not parsed_origin.path
        and not parsed_origin.query
        and not parsed_origin.fragment
    )
