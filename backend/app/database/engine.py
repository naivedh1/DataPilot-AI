"""Engine and connection construction.

Two distinct engines exist, and the distinction is a security control rather
than a convenience:

* the **read-only** engine authenticates as `datapilot_readonly`, a role with
  `SELECT` and nothing else. Every agent-generated query runs through it.
* the **admin** engine authenticates as `datapilot_admin` and is used only by
  schema management and seeding.

The read-only engine additionally sets a server-side `statement_timeout`, so a
runaway query is cancelled by PostgreSQL itself rather than depending on the
client staying alive to cancel it.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine

from app.core.config import Settings

#: Identifies the connection in `pg_stat_activity`, which makes it possible to
#: tell an agent query apart from a seeding job when inspecting a live cluster.
APP_NAME_READONLY = "datapilot-readonly"
APP_NAME_ADMIN = "datapilot-admin"


def to_libpq(dsn: str) -> str:
    """Convert a SQLAlchemy DSN to the plain libpq form psycopg expects.

    SQLAlchemy needs the `+psycopg` driver marker; `psycopg.connect()` does not
    accept it. Bootstrap talks to psycopg directly (it issues statements that
    cannot run inside a transaction), so it needs the stripped form.
    """
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def create_readonly_engine(settings: Settings) -> Engine:
    """Engine for executing user/agent SQL. Read-only role, enforced timeout."""
    return create_engine(
        settings.readonly_dsn,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={
            "application_name": APP_NAME_READONLY,
            # Belt and braces alongside the role grants: even a SELECT that
            # somehow reached a writable context could not write.
            "options": (
                f"-c statement_timeout={settings.sql_statement_timeout_ms}"
                " -c default_transaction_read_only=on"
            ),
        },
    )


def create_admin_engine(settings: Settings) -> Engine:
    """Engine for DDL and seeding. Never used to run user-supplied SQL."""
    return create_engine(
        settings.admin_dsn,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
        connect_args={"application_name": APP_NAME_ADMIN},
    )
