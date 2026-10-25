"""Per-test fresh database + client. FROZEN (CONTRACT §0)."""
import pytest
from fastapi.testclient import TestClient  # pyright: ignore[reportMissingImports]


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    monkeypatch.setenv("SOCIAL_MATCH_DB", path)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("BLOCK_SALT", "test-salt")
    monkeypatch.setenv("REGISTRATION_CODE", "testiva")
    # Tests never download a model: the app starts with the encoder off unless a test loads a fake.
    monkeypatch.setenv("ENCODER_ENABLED", "0")
    return path


@pytest.fixture()
def client(db_env):
    # Import AFTER env is patched: nothing in the spine caches config at import time.
    from server.app import app

    with TestClient(app) as c:
        yield c
