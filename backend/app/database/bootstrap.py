"""Cluster bootstrap: roles, database, and the read-only privilege boundary.

This module establishes the security model the rest of DataPilot AI depends on.
Application-side SQL validation (Phase 6) is the first line of defence; the
grants created here are the second, and the one that still holds if the first
is bypassed entirely.

Every statement is idempotent — running bootstrap twice is a no-op, never an
error and never destructive.

Identifiers and literals are composed with `psycopg.sql`, never with f-strings.
Role names and passwords come from configuration, and configuration is
attacker-influenced often enough that string-formatting them into DDL would be
a genuine injection vector.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg
from psycopg import sql

from app.core.config import Settings
from app.database.engine import to_libpq
from app.models import TABLE_LOAD_ORDER

logger = logging.getLogger(__name__)

#: Privileges the read-only role must never hold on any warehouse table.
FORBIDDEN_PRIVILEGES: tuple[str, ...] = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """What bootstrap actually changed, for the seeding summary."""

    database_created: bool
    admin_role_created: bool
    readonly_role_created: bool
    audit_role_created: bool
    grants_applied: bool


def _role_exists(conn: psycopg.Connection, role: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        return cur.fetchone() is not None


def _database_exists(conn: psycopg.Connection, database: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
        return cur.fetchone() is not None


def _ensure_login_role(conn: psycopg.Connection, role: str, password: str) -> bool:
    """Create a LOGIN role if absent; otherwise resync its password.

    Returns True when the role was created. Resyncing the password on an
    existing role keeps a regenerated .env working without a manual repair
    step, which is the common case when re-seeding a development machine.
    """
    if _role_exists(conn, role):
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        logger.info("role %s already existed; password resynchronised", role)
        return False

    conn.execute(
        sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )
    logger.info("created role %s", role)
    return True


def ensure_roles_and_database(settings: Settings) -> BootstrapReport:
    """Create the two roles and the warehouse database if they do not exist.

    Connects to the maintenance database because `CREATE DATABASE` and
    `CREATE ROLE` cannot run inside the database being created, and
    `CREATE DATABASE` cannot run inside a transaction block at all — hence
    autocommit.
    """
    admin_user = settings.postgres_admin_user
    readonly_user = settings.postgres_readonly_user
    audit_user = settings.postgres_audit_user
    database = settings.postgres_db

    with psycopg.connect(to_libpq(settings.superuser_maintenance_dsn), autocommit=True) as conn:
        admin_created = _ensure_login_role(
            conn, admin_user, settings.postgres_admin_password.get_secret_value()
        )
        readonly_created = _ensure_login_role(
            conn, readonly_user, settings.postgres_readonly_password.get_secret_value()
        )
        # The audit writer. Created here so it exists before the audit schema
        # is built; it receives INSERT on that schema and nothing else — see
        # `app/database/audit.py`.
        audit_created = _ensure_login_role(
            conn, audit_user, settings.postgres_audit_password.get_secret_value()
        )

        db_created = False
        if not _database_exists(conn, database):
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(admin_user)
                )
            )
            db_created = True
            logger.info("created database %s owned by %s", database, admin_user)

        # Only the owner may create objects; revoke the implicit rights that
        # every role would otherwise inherit through PUBLIC.
        conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        for role in (readonly_user, audit_user):
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
        conn.execute(
            sql.SQL("GRANT ALL ON DATABASE {} TO {}").format(
                sql.Identifier(database), sql.Identifier(admin_user)
            )
        )

    return BootstrapReport(
        database_created=db_created,
        admin_role_created=admin_created,
        readonly_role_created=readonly_created,
        audit_role_created=audit_created,
        grants_applied=False,
    )


def apply_readonly_grants(settings: Settings) -> None:
    """Grant exactly SELECT on the warehouse to the read-only role.

    Runs against the warehouse database as superuser, after the tables exist.

    The order matters. Revoking PUBLIC's rights on the schema first means the
    read-only role holds only what is granted to it explicitly, rather than
    whatever PUBLIC happens to carry.
    """
    readonly_user = settings.postgres_readonly_user
    admin_user = settings.postgres_admin_user

    with psycopg.connect(to_libpq(settings.superuser_dsn), autocommit=True) as conn:
        # No role may create objects in `public` except the schema owner.
        conn.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        conn.execute(
            sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(sql.Identifier(readonly_user))
        )

        # USAGE lets the role resolve names in the schema. It does not permit
        # creating anything.
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(readonly_user))
        )
        conn.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(
                sql.Identifier(readonly_user)
            )
        )

        # Tables created later by the admin role must inherit the same grant,
        # otherwise a future migration silently makes new data invisible.
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}"
            ).format(sql.Identifier(admin_user), sql.Identifier(readonly_user))
        )

        # Sequences are readable but not writable: the role can inspect
        # `last_value` but cannot call nextval() to burn ids.
        conn.execute(
            sql.SQL("GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                sql.Identifier(readonly_user)
            )
        )
        # The audit writer holds nothing on the warehouse — not even SELECT.
        # Its only purpose is INSERT into the audit schema, and a log writer
        # that could read business data would be a second way in.
        conn.execute(
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(
                sql.Identifier(settings.postgres_audit_user)
            )
        )
        conn.execute(
            sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(
                sql.Identifier(settings.postgres_audit_user)
            )
        )

        logger.info("read-only grants applied to %s", readonly_user)


def verify_readonly_privileges(settings: Settings) -> list[str]:
    """Assert the read-only role holds SELECT and nothing else.

    Returns a list of human-readable problems; empty means the boundary is
    intact. This inspects the catalog rather than attempting writes, so it is
    safe to run against a populated database.

    Catalog inspection is deliberately paired with the live write attempts in
    the integration tests: this proves the grants are *recorded* correctly, and
    those prove they are *enforced*.
    """
    readonly_user = settings.postgres_readonly_user
    problems: list[str] = []

    with psycopg.connect(to_libpq(settings.superuser_dsn)) as conn, conn.cursor() as cur:
        for table in TABLE_LOAD_ORDER:
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (readonly_user, table))
            row = cur.fetchone()
            if row is None or not row[0]:
                problems.append(f"{readonly_user} lacks SELECT on {table}")

            for privilege in FORBIDDEN_PRIVILEGES:
                cur.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (readonly_user, table, privilege),
                )
                row = cur.fetchone()
                if row is not None and row[0]:
                    problems.append(f"{readonly_user} unexpectedly holds {privilege} on {table}")

        # CREATE on the schema would let the role build scratch tables.
        cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (readonly_user,))
        row = cur.fetchone()
        if row is not None and row[0]:
            problems.append(f"{readonly_user} unexpectedly holds CREATE on schema public")

        # A superuser or role-creating account would bypass every grant above.
        cur.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = %s",
            (readonly_user,),
        )
        attrs = cur.fetchone()
        if attrs is None:
            problems.append(f"role {readonly_user} does not exist")
        else:
            is_super, can_create_db, can_create_role = attrs
            if is_super:
                problems.append(f"{readonly_user} is a SUPERUSER")
            if can_create_db:
                problems.append(f"{readonly_user} holds CREATEDB")
            if can_create_role:
                problems.append(f"{readonly_user} holds CREATEROLE")

    return problems
