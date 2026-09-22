"""Tests for the audit log that need no database.

The privilege boundary — which is the whole reason this is a separate role —
is proved in the integration suite, by attempting the writes and reads that
must fail. What is covered here is the shape of an entry, the truncation
limits, and the one behaviour that must hold whatever the database is doing:
a failed write does not fail the request.
"""

from __future__ import annotations

import psycopg
import pytest

from app.database import audit
from app.database.audit import (
    AUDIT_SCHEMA,
    AUDIT_TABLE,
    MAX_QUESTION_LENGTH,
    MAX_SQL_LENGTH,
    AuditEntry,
    _truncate,
)


class TestTruncation:
    def test_short_text_is_untouched(self):
        assert _truncate("hello", 100) == "hello"

    def test_long_text_is_cut_and_marked(self):
        """Marked rather than silently cut: a truncated query that looks whole
        would send someone debugging in the wrong direction."""
        result = _truncate("x" * 500, 100)
        assert result.startswith("x" * 100)
        assert "truncated" in result

    def test_limits_are_large_enough_for_real_input(self):
        # A diagnostic run concatenates six statements into `sql_text`.
        assert MAX_SQL_LENGTH > 6 * 1_000
        assert MAX_QUESTION_LENGTH >= 1_000


class TestEntry:
    def test_only_three_fields_are_required(self):
        """A run that failed early still has to be loggable — that is exactly
        when the log matters most."""
        entry = AuditEntry(request_id="r1", question="q", status="error")
        assert entry.tables_used == ()
        assert entry.detail == {}
        assert entry.llm_simulated is False

    def test_detail_defaults_are_not_shared_between_entries(self):
        first = AuditEntry(request_id="a", question="q", status="success")
        second = AuditEntry(request_id="b", question="q", status="success")
        first.detail["x"] = 1
        assert second.detail == {}


class TestFailureIsNeverFatal:
    """An answered question must not become an error because the log was
    unreachable. This is the module's stated contract."""

    def test_a_database_error_is_swallowed(self, monkeypatch):
        def explode(*args: object, **kwargs: object) -> None:
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(audit.psycopg, "connect", explode)
        assert audit.record(AuditEntry(request_id="r", question="q", status="success")) is False

    def test_an_os_error_is_swallowed(self, monkeypatch):
        def explode(*args: object, **kwargs: object) -> None:
            raise OSError("host unreachable")

        monkeypatch.setattr(audit.psycopg, "connect", explode)
        assert audit.record(AuditEntry(request_id="r", question="q", status="success")) is False

    def test_an_unexpected_error_is_not_swallowed(self, monkeypatch):
        """Only connection-shaped failures are tolerated. A bug in this module
        should surface in tests rather than be hidden by the same except."""

        def explode(*args: object, **kwargs: object) -> None:
            raise ValueError("programming error")

        monkeypatch.setattr(audit.psycopg, "connect", explode)
        with pytest.raises(ValueError):
            audit.record(AuditEntry(request_id="r", question="q", status="success"))


class TestStatements:
    def test_the_table_is_created_in_the_audit_schema(self):
        rendered = audit.CREATE_TABLE.as_string(None)
        assert f'"{AUDIT_SCHEMA}"."{AUDIT_TABLE}"' in rendered

    def test_the_insert_names_every_stored_column(self):
        rendered = audit._INSERT.as_string(None)
        for column in (
            "request_id",
            "question",
            "status",
            "confidence",
            "validation",
            "tables_used",
            "detail",
        ):
            assert column in rendered

    def test_creation_is_idempotent(self):
        """Seeding runs repeatedly on a development machine."""
        assert "IF NOT EXISTS" in audit.CREATE_SCHEMA.as_string(None)
        assert "IF NOT EXISTS" in audit.CREATE_TABLE.as_string(None)
        for index in audit.INDEXES:
            assert "IF NOT EXISTS" in index.as_string(None)
