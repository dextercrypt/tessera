"""Shared fixtures for the tess test suite.

Imports src/tess.py as a module and redirects its per-platform data/config
directories into per-test temp dirs, so no test ever touches the real
tess installation (or live session) of the machine running the suite.
"""
import base64
import json
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

import tess  # noqa: E402


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Sandbox tess's data and config dirs into a per-test temp directory.

    All path helpers (token_path, pid_path, config_dir, ...) call data_dir()
    / config_dir() at call time, so patching the two module attributes is
    enough to redirect every file tess reads or writes.
    """
    monkeypatch.setattr(tess, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(tess, "config_dir", lambda: tmp_path / "config")
    monkeypatch.delenv("TESS_CONFIG", raising=False)
    return tmp_path


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture
def make_jwt():
    """Build an unsigned JWT with the given payload (tess never verifies
    signatures locally — STS does — so a dummy signature part suffices)."""
    def _make(payload: dict) -> str:
        header = _b64url({"alg": "RS256", "typ": "JWT"})
        return f"{header}.{_b64url(payload)}.dummy-signature"
    return _make


@pytest.fixture
def valid_config():
    """A minimal config dict that passes validate_config()."""
    return {
        "tenant_id": "11111111-2222-3333-4444-555555555555",
        "client_id": "66666666-7777-8888-9999-000000000000",
        "role_arn": "arn:aws:iam::123456789012:role/dev-role",
        "region": "us-east-1",
    }


@pytest.fixture
def write_config(dirs, valid_config):
    """Write a config file into the sandbox and return its path."""
    def _write(path: Path | None = None, **overrides):
        data = {**valid_config, **overrides}
        # None means "remove the key" so tests can express absence.
        data = {k: v for k, v in data.items() if v is not None}
        if path is None:
            path = dirs / "config" / tess.CONFIG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path
    return _write
