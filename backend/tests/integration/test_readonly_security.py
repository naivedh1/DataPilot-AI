"""The read-only boundary, tested against the live database.

This is the security control the whole project rests on. Phase 7's SQL validator
is the first line of defence; these grants are the second, and the one that
still holds if the validator is bypassed, buggy, or defeated by a prompt
injection nobody anticipated.

Every test here connects as `datapilot_readonly` — the role the agent will
actually use — and attempts a real write against a real database. Nothing is
mocked, because a mock of a permission system proves nothing about the
permission system.
"""

from __future__ import annotations

import psycopg
import pytest

from app.core.config import Settings
from app.database.bootstrap import FORBIDDEN_PRIVILEGES, verify_readonly_privileges
from app.models import TABLE_LOAD_ORDER

pytestmark = pytest.mark.integration


def _expect_denied(conn: psycopg.Connection, statement: str) -> psycopg.Error:
    """Run a statement that must fail, and return the error it raised."""
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
    except psycopg.Error as error:
        conn.rollback()
        return error
    conn.rollback()
    pytest.fail(f"SECURITY: statement was permitted but must not be: {statement}")


class TestReadIsPermitted:
    """The role must still be able to do its job."""

    def test_select_succeeds(self, readonly_conn, seeded):
        with readonly_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM orders")
            assert cur.fetchone()[0] > 0

    def test_joins_and_aggregates_succeed(self, readonly_conn, seeded):
        with readonly_conn.cursor() as cur:
            cur.execute(
                "SELECT r.name, SUM(o.total_amount) FROM orders o "
                "JOIN regions r ON r.id = o.shipping_region_id "
                "WHERE o.status = 'completed' GROUP BY r.name"
            )
            assert len(cur.fetchall()) == 12

    def test_window_functions_succeed(self, readonly_conn, seeded):
        """Analytical SQL must work, or the agent cannot answer real questions."""
        with readonly_conn.cursor() as cur:
            cur.execute(
                "SELECT date_trunc('month', order_date) AS m, SUM(total_amount), "
                "LAG(SUM(total_amount)) OVER (ORDER BY date_trunc('month', order_date)) "
                "FROM orders WHERE status='completed' GROUP BY m ORDER BY m LIMIT 5"
            )
            assert len(cur.fetchall()) == 5

    def test_ctes_succeed(self, readonly_conn, seeded):
        with readonly_conn.cursor() as cur:
            cur.execute(
                "WITH monthly AS (SELECT date_trunc('month', order_date) m, "
                "SUM(total_amount) rev FROM orders GROUP BY 1) "
                "SELECT count(*) FROM monthly"
            )
            assert cur.fetchone()[0] == 24


class TestWritesAreDenied:
    """Every mutating statement must be refused by PostgreSQL itself."""

    def test_insert_is_denied(self, readonly_conn):
        error = _expect_denied(
            readonly_conn,
            "INSERT INTO regions (name, country, territory) VALUES ('Hacked', 'Nowhere', 'None')",
        )
        assert isinstance(error, psycopg.errors.InsufficientPrivilege)

    def test_update_is_denied(self, readonly_conn):
        error = _expect_denied(readonly_conn, "UPDATE orders SET total_amount = 0")
        assert isinstance(error, psycopg.errors.InsufficientPrivilege)

    def test_delete_is_denied(self, readonly_conn):
        error = _expect_denied(readonly_conn, "DELETE FROM customers")
        assert isinstance(error, psycopg.errors.InsufficientPrivilege)

    def test_truncate_is_denied(self, readonly_conn):
        error = _expect_denied(readonly_conn, "TRUNCATE order_items")
        assert isinstance(error, psycopg.errors.InsufficientPrivilege)

    @pytest.mark.parametrize("table", sorted(TABLE_LOAD_ORDER))
    def test_no_table_accepts_a_delete(self, readonly_conn, table):
        """Checked per table: a single missed REVOKE would leave one table
        writable, which a table-by-table test catches and a spot check does not.
        """
        _expect_denied(readonly_conn, f"DELETE FROM {table}")  # noqa: S608


class TestSchemaChangesAreDenied:
    def test_drop_table_is_denied(self, readonly_conn):
        _expect_denied(readonly_conn, "DROP TABLE orders")

    def test_alter_table_is_denied(self, readonly_conn):
        _expect_denied(readonly_conn, "ALTER TABLE orders ADD COLUMN injected int")

    def test_create_table_is_denied(self, readonly_conn):
        """CREATE would let an attacker build scratch tables to stage data in."""
        _expect_denied(readonly_conn, "CREATE TABLE exfiltrated (data text)")

    def test_create_index_is_denied(self, readonly_conn):
        _expect_denied(readonly_conn, "CREATE INDEX evil_idx ON orders (status)")

    def test_drop_index_is_denied(self, readonly_conn):
        _expect_denied(readonly_conn, "DROP INDEX ix_orders_order_date")

    def test_create_function_is_denied(self, readonly_conn):
        """A function would be a durable foothold executing under another role."""
        _expect_denied(
            readonly_conn,
            "CREATE FUNCTION evil() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql",
        )

    def test_create_view_is_denied(self, readonly_conn):
        _expect_denied(readonly_conn, "CREATE VIEW evil AS SELECT * FROM customers")


class TestPrivilegeEscalationIsDenied:
    def test_granting_itself_privileges_does_not_escalate(self, readonly_conn, db_settings):
        """A self-GRANT must not actually grant anything.

        PostgreSQL does *not* raise here: granting a privilege you do not hold
        emits a warning and grants nothing, so the statement appears to succeed.
        Asserting on an exception would therefore be asserting the wrong thing.
        What matters is the effect — that the privilege set is unchanged and a
        real write is still refused afterwards.
        """
        role = db_settings.postgres_readonly_user
        with readonly_conn.cursor() as cur:
            cur.execute("SELECT has_table_privilege(current_user,'orders','INSERT')")
            assert cur.fetchone()[0] is False

            cur.execute(f"GRANT ALL ON orders TO {role}")

            cur.execute("SELECT has_table_privilege(current_user,'orders','INSERT')")
            assert cur.fetchone()[0] is False, "SECURITY: self-GRANT escalated"
        readonly_conn.rollback()

        # And the write itself is still refused.
        error = _expect_denied(
            readonly_conn,
            "INSERT INTO regions (name, country, territory) VALUES ('escalated', 'x', 'y')",
        )
        assert isinstance(error, psycopg.errors.InsufficientPrivilege)

    def test_creating_a_role_is_denied(self, readonly_conn):
        _expect_denied(readonly_conn, "CREATE ROLE backdoor LOGIN PASSWORD 'x'")

    def test_altering_its_own_role_is_denied(self, readonly_conn, db_settings):
        _expect_denied(
            readonly_conn,
            f"ALTER ROLE {db_settings.postgres_readonly_user} SUPERUSER",
        )

    def test_creating_a_database_is_denied(self, readonly_conn):
        _expect_denied(readonly_conn, "CREATE DATABASE exfiltration")

    def test_the_role_is_not_a_superuser(self, readonly_conn):
        """A superuser would bypass every grant above without triggering one."""
        with readonly_conn.cursor() as cur:
            cur.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
            is_super, create_db, create_role, bypass_rls = cur.fetchone()
        assert not is_super
        assert not create_db
        assert not create_role
        assert not bypass_rls


class TestDataExfiltrationIsLimited:
    def test_copy_to_a_server_file_is_denied(self, readonly_conn):
        """Server-side COPY TO writes files as the postgres OS user. It is
        restricted to superusers, but asserting it keeps the guarantee explicit.
        """
        _expect_denied(readonly_conn, "COPY customers TO '/tmp/stolen.csv'")

    def test_copy_from_a_server_file_is_denied(self, readonly_conn):
        _expect_denied(readonly_conn, "COPY customers FROM '/etc/passwd'")

    def test_reading_other_role_passwords_is_denied(self, readonly_conn):
        """`pg_authid` holds password hashes and is superuser-only."""
        _expect_denied(readonly_conn, "SELECT rolpassword FROM pg_authid")

    def test_reading_server_settings_does_not_expose_credentials(self, readonly_conn, db_settings):
        """`pg_settings` is world-readable, so confirm what it actually leaks.

        It does contain a `password`-named row — `password_encryption`, whose
        value is an algorithm name such as 'scram-sha-256'. That is not a
        credential. The meaningful assertion is that no *real* secret appears,
        so this checks for the configured passwords themselves.
        """
        secrets = {
            db_settings.postgres_readonly_password.get_secret_value(),
            db_settings.postgres_admin_password.get_secret_value(),
        }
        with readonly_conn.cursor() as cur:
            cur.execute("SELECT name, setting FROM pg_settings")
            rows = cur.fetchall()

        blob = " ".join(f"{name}={setting}" for name, setting in rows)
        for secret in secrets:
            if secret:
                assert secret not in blob, "SECURITY: a password is readable"


class TestCatalogPrivileges:
    """The grants must be *recorded* correctly, not only enforced.

    Pairing catalog inspection with the live write attempts above covers both
    halves: these assert what the database believes, those assert what it does.
    """

    def test_verifier_reports_no_problems(self, db_settings: Settings, _require_database):
        assert verify_readonly_privileges(db_settings) == []

    @pytest.mark.parametrize("table", sorted(TABLE_LOAD_ORDER))
    def test_select_is_granted_on_every_table(self, readonly_conn, table):
        with readonly_conn.cursor() as cur:
            cur.execute("SELECT has_table_privilege(current_user, %s, 'SELECT')", (table,))
            assert cur.fetchone()[0] is True

    @pytest.mark.parametrize("privilege", FORBIDDEN_PRIVILEGES)
    def test_no_write_privilege_on_orders(self, readonly_conn, privilege):
        with readonly_conn.cursor() as cur:
            cur.execute("SELECT has_table_privilege(current_user, 'orders', %s)", (privilege,))
            assert cur.fetchone()[0] is False, f"unexpectedly holds {privilege}"

    def test_no_create_privilege_on_the_schema(self, readonly_conn):
        with readonly_conn.cursor() as cur:
            cur.execute("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            assert cur.fetchone()[0] is False

    def test_connect_is_granted(self, readonly_conn, db_settings):
        with readonly_conn.cursor() as cur:
            cur.execute(
                "SELECT has_database_privilege(current_user, %s, 'CONNECT')",
                (db_settings.postgres_db,),
            )
            assert cur.fetchone()[0] is True

    def test_sequences_cannot_be_advanced(self, readonly_conn):
        """USAGE on a sequence would permit nextval(), burning ids."""
        with readonly_conn.cursor() as cur:
            cur.execute(
                "SELECT has_sequence_privilege(current_user, "
                "pg_get_serial_sequence('orders','id'), 'USAGE')"
            )
            assert cur.fetchone()[0] is False


class TestSessionSafeguards:
    def test_transactions_default_to_read_only(self, db_settings, _require_database):
        """Belt and braces alongside the grants: the engine also opens sessions
        with `default_transaction_read_only`, so a write fails even before the
        privilege check."""
        from sqlalchemy import text

        from app.database.engine import create_readonly_engine

        engine = create_readonly_engine(db_settings)
        try:
            with engine.connect() as conn:
                value = conn.execute(text("SHOW default_transaction_read_only"))
                assert value.scalar() == "on"
        finally:
            engine.dispose()

    def test_statement_timeout_is_applied(self, db_settings, _require_database):
        """A runaway query must be cancelled by the server, not left depending
        on the client staying alive to cancel it."""
        from sqlalchemy import text

        from app.database.engine import create_readonly_engine

        engine = create_readonly_engine(db_settings)
        try:
            with engine.connect() as conn:
                timeout = conn.execute(text("SHOW statement_timeout")).scalar()
            assert timeout not in (None, "0"), "no statement timeout configured"
        finally:
            engine.dispose()
