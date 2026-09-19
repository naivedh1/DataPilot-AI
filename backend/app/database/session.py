"""Engine and session lifecycle.

Engines are expensive (they own a connection pool) and must be created once per
process, not per request. They are cached here and disposed at shutdown.

Two separate engines exist because the distinction is a security control:
`get_readonly_session()` authenticates as a role that holds `SELECT` and nothing
else, and is the only path agent-generated SQL may take.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.database.engine import create_admin_engine, create_readonly_engine

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_readonly_engine() -> Engine:
    """Process-wide read-only engine. Used for every user-facing query."""
    return create_readonly_engine(get_settings())


@lru_cache(maxsize=1)
def get_admin_engine() -> Engine:
    """Process-wide admin engine. Schema and seeding only, never user SQL."""
    return create_admin_engine(get_settings())


@lru_cache(maxsize=1)
def _readonly_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_readonly_engine(), autoflush=False, expire_on_commit=False)


@contextmanager
def readonly_session() -> Iterator[Session]:
    """A session bound to the read-only role.

    Always rolls back on exit. There is nothing to commit — the role cannot
    write — and an explicit rollback releases any transaction snapshot the
    connection is holding rather than returning it to the pool mid-transaction.
    """
    session = _readonly_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def dispose_engines() -> None:
    """Close all pooled connections. Called on application shutdown."""
    for factory in (get_readonly_engine, get_admin_engine):
        if factory.cache_info().currsize:
            factory().dispose()
            logger.info("disposed engine: %s", factory.__name__)
    get_readonly_engine.cache_clear()
    get_admin_engine.cache_clear()
    _readonly_sessionmaker.cache_clear()


def check_database_health(settings: Settings | None = None) -> tuple[bool, str]:
    """Probe the read-only connection.

    Returns `(healthy, detail)` rather than raising: a health endpoint must
    report a failure, not become one.
    """
    settings = settings or get_settings()
    try:
        with readonly_session() as session:
            version = session.execute(text("SELECT version()")).scalar_one()
            session.execute(text("SELECT 1 FROM orders LIMIT 1"))
    except Exception as error:
        # The message may embed a DSN, so it is logged and never returned.
        logger.warning("database health check failed: %s", error)
        return False, "database unreachable"

    short = str(version).split(" on ")[0]
    return True, short
