"""Shared pytest fixtures.

The backend must be importable and testable without a live database or a live
Gemini key, so tests construct `Settings` explicitly rather than relying on
whatever happens to be in the developer's .env.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import AppEnv, Settings, get_settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Deterministic test settings with placeholder credentials."""
    return Settings(
        app_env=AppEnv.TEST,
        gemini_api_key=SecretStr("test-key-not-real"),
        postgres_readonly_password=SecretStr("test-ro-password"),
        postgres_admin_password=SecretStr("test-admin-password"),
        cors_allowed_origins=["http://localhost:5173"],
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A TestClient bound to an app built from the test settings."""
    # The health route resolves settings through get_settings(); point that at
    # the test instance for the lifetime of the fixture.
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides = {}
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark every test in tests/unit/ as `unit` automatically.

    Marking by hand means a new file eventually forgets, and `pytest -m unit`
    then silently runs fewer tests than it appears to — or, as happened here,
    none at all while still reporting success. Deriving the marker from the
    directory makes the convention self-enforcing.

    Integration tests declare their own marker, since they additionally need
    the database fixtures.
    """
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        if "/tests/unit/" in path and "integration" not in item.keywords:
            item.add_marker(pytest.mark.unit)
