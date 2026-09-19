"""Fixtures for tests that talk to a real PostgreSQL instance.

These are marked `integration` and skip cleanly when no database is reachable,
so `pytest -m unit` and CI both stay green on a machine with no warehouse.
Skipping is only ever for *absence* of a database — never for a failure.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from app.core.config import Settings, get_settings
from app.database.engine import to_libpq

pytestmark = pytest.mark.integration


def _can_connect(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=5):
            return True
    except psycopg.Error:
        return False


@pytest.fixture(scope="session")
def db_settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def _require_database(db_settings: Settings) -> None:
    """Skip the whole integration suite if the warehouse is unreachable."""
    if not _can_connect(to_libpq(db_settings.admin_dsn)):
        pytest.skip(
            "PostgreSQL warehouse unavailable — run `python -m scripts.seed_database`",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
def admin_conn(db_settings: Settings, _require_database: None) -> Iterator[psycopg.Connection]:
    """Read/write connection as `datapilot_admin`. Never used to test security."""
    with psycopg.connect(to_libpq(db_settings.admin_dsn)) as conn:
        yield conn


@pytest.fixture
def readonly_conn(db_settings: Settings, _require_database: None) -> Iterator[psycopg.Connection]:
    """Connection as `datapilot_readonly` — the role the agent will use.

    Function-scoped: the security tests deliberately provoke errors, which
    aborts the transaction, and a shared connection would leak that state into
    the next test.
    """
    with psycopg.connect(to_libpq(db_settings.readonly_dsn)) as conn:
        yield conn


@pytest.fixture(scope="session")
def seeded(admin_conn: psycopg.Connection) -> None:
    """Skip if the warehouse exists but holds no data."""
    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM orders")
        row = cur.fetchone()
    if not row or row[0] == 0:
        pytest.skip("warehouse is empty — run `python -m scripts.seed_database`")
