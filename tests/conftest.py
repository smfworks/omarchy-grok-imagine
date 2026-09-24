from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from omarchy_imagine.app import create_app


@pytest.fixture(autouse=True)
def _no_live_imagine_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)


@pytest.fixture
def app(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OMARCHY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMARCHY_SYNC_JOBS", "1")
    return create_app()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)
