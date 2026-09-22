"""The audit log's privilege boundary, proved against a live cluster.

The audit log adds a write path to a system whose defining property is that
generated SQL runs under a role that cannot write. That trade is only
defensible if the writer is genuinely confined, so these tests attempt every
access that must fail and assert it does.

Catalog inspection would not be enough here. A grant can be recorded correctly
and still be reachable another way — through PUBLIC, through role inheritance,
through a default privilege someone added later. Only attempting the operation
proves the boundary is enforced rather than merely configured.
"""

from __future__ import annotations

import psycopg
import pytest

from app.core.config import get_settings
from app.database import audit
from app.database.audit import AuditEntry
from app.database.engine import to_libpq

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def settings():
    return get_settings()


def _denied(dsn: str, statement: str) -> bool:
    """Whether `statement` is refused for want of privilege."""
    try:
        with psycopg.connect(to_libpq(dsn), connect_timeout=5) as conn:
            conn.execute(statement)
    except psycopg.errors.InsufficientPrivilege:
        return True
    except psycopg.errors.UndefinedTable:
        # Unreachable names are also a refusal: the role cannot see the object.
        return True
    else:
        return False


class TestTheAuditWriterIsConfined:
    def test_it_can_insert_into_the_log(self, settings, seeded):
        assert audit.record(
            AuditEntry(
                request_id="test-boundary",
                question="probe",
                status="success",
                confidence="high",
            ),
            settings,
        )

    def test_it_cannot_read_the_log_back(self, settings, seeded):
        """INSERT without SELECT is what makes the trail worth having: the
        writer cannot check what it recorded, so it cannot selectively omit."""
        assert _denied(settings.audit_dsn, "SELECT count(*) FROM audit.query_log")

    def test_it_cannot_update_or_delete_its_own_rows(self, settings, seeded):
        assert _denied(settings.audit_dsn, "UPDATE audit.query_log SET answer = 'x'")
        assert _denied(settings.audit_dsn, "DELETE FROM audit.query_log")

    def test_it_cannot_read_the_warehouse(self, settings, seeded):
        """A log writer that could reach business data would be a second way
        in. It holds no grant on the warehouse at all — not even SELECT."""
        for statement in (
            "SELECT count(*) FROM orders",
            "SELECT count(*) FROM customers",
            "SELECT count(*) FROM refunds",
        ):
            assert _denied(settings.audit_dsn, statement), statement

    def test_it_cannot_write_the_warehouse(self, settings, seeded):
        assert _denied(settings.audit_dsn, "DELETE FROM orders")
        assert _denied(settings.audit_dsn, "UPDATE orders SET status = 'completed'")


class TestTheReadOnlyRoleCannotReachTheLog:
    def test_it_cannot_read_the_audit_log(self, settings, seeded):
        """Generated SQL runs as this role. Without this, a model could be
        talked into mining the log for other people's questions."""
        assert _denied(settings.readonly_dsn, "SELECT count(*) FROM audit.query_log")

    def test_it_cannot_write_the_audit_log(self, settings, seeded):
        assert _denied(
            settings.readonly_dsn,
            "INSERT INTO audit.query_log (request_id, question, status) "
            "VALUES ('x', 'x', 'success')",
        )

    def test_it_can_still_read_the_warehouse(self, settings, seeded):
        """The boundary must not have been achieved by breaking the thing the
        role exists for."""
        assert not _denied(settings.readonly_dsn, "SELECT count(*) FROM orders")


class TestEntriesLand:
    def test_a_recorded_entry_is_retrievable_by_an_admin(self, settings, seeded, admin_conn):
        request_id = "test-roundtrip"
        assert audit.record(
            AuditEntry(
                request_id=request_id,
                question="how much did we refund?",
                status="success",
                confidence="medium",
                validation="passed",
                tables_used=("refunds",),
                row_count=1,
                detail={"note": "round trip"},
            ),
            settings,
        )

        with psycopg.connect(to_libpq(settings.superuser_dsn)) as conn:
            row = conn.execute(
                "SELECT question, status, confidence, validation, tables_used, detail "
                "FROM audit.query_log WHERE request_id = %s ORDER BY id DESC LIMIT 1",
                (request_id,),
            ).fetchone()
            conn.execute("DELETE FROM audit.query_log WHERE request_id = %s", (request_id,))
            conn.commit()

        assert row is not None
        assert row[0] == "how much did we refund?"
        assert row[2] == "medium"
        assert row[4] == ["refunds"]
        assert row[5] == {"note": "round trip"}
