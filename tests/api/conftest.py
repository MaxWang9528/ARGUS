"""Fixtures for the dashboard API: a demo App (fakes, no server) and a TestClient."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from livewatcher.api.server import create_app
from livewatcher.app import App
from livewatcher.core.config import ConfigStore


@pytest.fixture
def lw(store: ConfigStore) -> Iterator[App]:
    app = App(store, demo=True, serve=False)
    yield app
    app.db.close()


@pytest.fixture
def client(lw: App) -> Iterator[TestClient]:
    """One event loop for the whole test, so tasks started by a request keep running."""
    with TestClient(create_app(lw)) as c:
        yield c


@pytest.fixture
def collecting(client: TestClient, lw: App) -> Iterator[None]:
    """Run the App's rate and log collector on the client's loop for the test."""
    future = client.portal.start_task_soon(lw._collect)
    yield
    future.cancel()
