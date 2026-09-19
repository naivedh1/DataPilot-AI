"""Tests for the guarded query executor.

Every agent-generated query in the system passes through `execute_readonly`, so
these cover the safeguards that call sites must never be able to forget.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.core.exceptions import QueryTimeoutError, SQLExecutionError
from app.database.executor import execute_readonly
from app.database.session import check_database_health

pytestmark = pytest.mark.integration


class TestSuccessfulExecution:
    def test_returns_columns_rows_and_metadata(self, seeded):
        result = execute_readonly(
            "SELECT status, count(*) AS n FROM orders GROUP BY status ORDER BY n DESC"
        )
        assert result.columns == ["status", "n"]
        assert result.row_count == 4
        assert result.duration_ms > 0
        assert not result.truncated
        assert result.rows[0][0] == "completed"

    def test_preserves_decimal_precision(self, seeded):
        """Money must arrive as Decimal, not float. Converting earlier would
        reintroduce exactly the drift NUMERIC columns exist to prevent."""
        result = execute_readonly(
            "SELECT SUM(total_amount) AS revenue FROM orders WHERE status='completed'"
        )
        assert isinstance(result.rows[0][0], Decimal)

    def test_to_records_is_json_serialisable(self, seeded):
        import json

        result = execute_readonly("SELECT order_date, total_amount FROM orders ORDER BY id LIMIT 3")
        records = result.to_records()
        assert len(records) == 3
        assert set(records[0]) == {"order_date", "total_amount"}
        json.dumps(records)  # must not raise
        assert isinstance(records[0]["total_amount"], float)
        assert isinstance(records[0]["order_date"], str)

    def test_empty_result_is_not_an_error(self, seeded):
        result = execute_readonly("SELECT id FROM orders WHERE 1 = 0")
        assert result.row_count == 0
        assert result.is_empty
        assert result.columns == ["id"]

    def test_window_functions_and_ctes_execute(self, seeded):
        result = execute_readonly(
            """
            WITH monthly AS (
                SELECT date_trunc('month', order_date) AS m, SUM(total_amount) AS rev
                FROM orders WHERE status = 'completed' GROUP BY 1
            )
            SELECT m, rev, LAG(rev) OVER (ORDER BY m) FROM monthly ORDER BY m
            """
        )
        assert result.row_count == 24


class TestRowCap:
    def test_row_cap_is_applied(self, seeded):
        result = execute_readonly("SELECT id FROM orders", max_rows=10)
        assert result.row_count == 10
        assert result.truncated

    def test_truncation_flag_is_false_when_under_the_cap(self, seeded):
        result = execute_readonly("SELECT id FROM orders LIMIT 3", max_rows=10)
        assert result.row_count == 3
        assert not result.truncated

    def test_cap_applies_to_a_query_that_already_has_its_own_limit(self, seeded):
        """The cap works by stopping the cursor, not by rewriting SQL, so a
        query with its own LIMIT (or a UNION, or a CTE) is handled correctly
        rather than by injecting a second LIMIT."""
        result = execute_readonly("SELECT id FROM orders LIMIT 500", max_rows=25)
        assert result.row_count == 25
        assert result.truncated

    def test_default_cap_comes_from_settings(self, seeded):
        settings = get_settings()
        result = execute_readonly("SELECT id FROM orders")
        assert result.row_count <= settings.sql_max_result_rows

    def test_cap_does_not_prevent_correct_aggregates(self, seeded):
        """Aggregation happens in the database, so the cap limits the rows
        returned — never the rows considered."""
        result = execute_readonly("SELECT count(*) FROM orders", max_rows=1)
        assert result.rows[0][0] == 50_000


class TestErrorTranslation:
    def test_missing_table_message_is_surfaced_for_repair(self, seeded):
        """The database's own message is the most useful possible feedback to
        the SQL-generation retry loop, and leaks nothing."""
        with pytest.raises(SQLExecutionError) as caught:
            execute_readonly("SELECT * FROM no_such_table")
        assert "no_such_table" in caught.value.safe_message
        assert "no_such_table" in caught.value.detail

    def test_missing_column_message_is_surfaced_for_repair(self, seeded):
        with pytest.raises(SQLExecutionError) as caught:
            execute_readonly("SELECT no_such_column FROM orders")
        assert "no_such_column" in caught.value.safe_message

    def test_syntax_error_message_is_surfaced(self, seeded):
        with pytest.raises(SQLExecutionError) as caught:
            execute_readonly("SELECT FROM WHERE")
        assert "syntax" in caught.value.safe_message.lower()

    @pytest.mark.parametrize(
        "statement",
        [
            "INSERT INTO regions (name, country, territory) VALUES ('a','b','c')",
            "UPDATE orders SET total_amount = 0",
            "DELETE FROM customers",
            "DROP TABLE orders",
            "CREATE TABLE evil (id int)",
        ],
    )
    def test_write_attempts_get_a_generic_safe_message(self, seeded, statement):
        """A privilege error must not echo the database's message back: it can
        name roles and schemas the user has no business seeing."""
        with pytest.raises(SQLExecutionError) as caught:
            execute_readonly(statement)
        assert caught.value.safe_message == "Only read-only queries are permitted."

    def test_safe_message_never_contains_credentials(self, seeded):
        settings = get_settings()
        password = settings.postgres_readonly_password.get_secret_value()
        with pytest.raises(SQLExecutionError) as caught:
            execute_readonly("SELECT * FROM no_such_table")
        assert password not in caught.value.safe_message
        assert password not in caught.value.detail

    def test_statement_timeout_raises_a_timeout_error(self, seeded):
        """A runaway query is cancelled by PostgreSQL, not left hanging."""
        with pytest.raises(QueryTimeoutError) as caught:
            # pg_sleep far exceeds the configured statement_timeout.
            execute_readonly("SELECT pg_sleep(30)")
        assert "too long" in caught.value.safe_message.lower()

    def test_timeout_is_a_kind_of_execution_error(self, seeded):
        """Callers handling SQLExecutionError must also catch timeouts."""
        with pytest.raises(SQLExecutionError):
            execute_readonly("SELECT pg_sleep(30)")


class TestHealth:
    def test_health_check_reports_healthy(self, seeded):
        healthy, detail = check_database_health()
        assert healthy
        assert "PostgreSQL" in detail

    def test_health_check_does_not_leak_a_dsn(self):
        """A failure message can embed the full connection string."""
        settings = get_settings()
        _, detail = check_database_health()
        assert settings.postgres_readonly_password.get_secret_value() not in detail


class TestSessionIsolation:
    def test_a_failed_query_does_not_poison_the_next_one(self, seeded):
        """Sessions are per-call and rolled back on exit, so an aborted
        transaction cannot leak into a later request."""
        with pytest.raises(SQLExecutionError):
            execute_readonly("SELECT * FROM no_such_table")

        result = execute_readonly("SELECT count(*) FROM orders")
        assert result.rows[0][0] == 50_000

    def test_repeated_execution_reuses_the_pool(self, seeded):
        """Engines are cached per process; a fresh pool per query would make
        every request pay connection setup."""
        for _ in range(5):
            assert execute_readonly("SELECT 1").row_count == 1
