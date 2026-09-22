"""The audit log: every analytical request, and what the system did with it.

Why this is a separate role and schema
--------------------------------------
The application connects as `datapilot_readonly`, and that is the security
model's entire point: SQL written by a language model runs under a role that
physically cannot write. Adding an audit log introduces a write path, which is
a hole in that model. The hole is made as narrow as it can be:

* a dedicated `datapilot_audit` role, used by nothing else;
* it holds INSERT on the `audit` schema only;
* it holds **no grant at all** on the warehouse — not even SELECT;
* the read-only role, in turn, cannot read the audit schema, so a generated
  query cannot mine the log for the questions other people asked.

The alternative was writing JSONL to disk, which keeps the read-only boundary
perfectly intact. It was rejected because the log's stated purpose is
supporting debugging and evaluation, and a log you cannot query with SQL does
not serve that. A narrowly-scoped writer is the honest trade, and it is
written down here so it can be argued with.

Failure behaviour
-----------------
**An audit failure never fails a request.** A user who asked a question and
got an answer must not see an error because the log was unavailable; the
answer was still correct. Failures are logged and swallowed, and the
`audit_write_failed` counter is the signal that something needs attention.

That is a deliberate choice against durability. In a regulated setting where
the audit trail is the product, the correct behaviour is the opposite — refuse
the request. This system is an analytics assistant, so availability wins.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql

from app.core.config import Settings, get_settings
from app.database.engine import to_libpq

logger = logging.getLogger(__name__)

AUDIT_SCHEMA = "audit"
AUDIT_TABLE = "query_log"

#: Longest question stored. A pasted document as a "question" should not be
#: able to bloat the log, and the first 2,000 characters identify any real one.
MAX_QUESTION_LENGTH = 2_000

#: Longest SQL stored. A diagnostic run concatenates six statements, so this is
#: generous; beyond it the query is truncated with a marker rather than lost.
MAX_SQL_LENGTH = 20_000

CREATE_SCHEMA = sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(AUDIT_SCHEMA))

CREATE_TABLE = sql.SQL(
    """
    CREATE TABLE IF NOT EXISTS {}.{} (
        id              BIGSERIAL PRIMARY KEY,
        request_id      TEXT        NOT NULL,
        conversation_id TEXT        NOT NULL DEFAULT '',
        asked_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        question        TEXT        NOT NULL,
        intent          TEXT        NOT NULL DEFAULT '',
        status          TEXT        NOT NULL,
        sql_text        TEXT        NOT NULL DEFAULT '',
        tables_used     TEXT[]      NOT NULL DEFAULT '{{}}',
        row_count       INTEGER     NOT NULL DEFAULT 0,
        retries         INTEGER     NOT NULL DEFAULT 0,
        confidence      TEXT        NOT NULL DEFAULT '',
        validation      TEXT        NOT NULL DEFAULT '',
        answer          TEXT        NOT NULL DEFAULT '',
        error_code      TEXT        NOT NULL DEFAULT '',
        model           TEXT        NOT NULL DEFAULT '',
        llm_calls       INTEGER     NOT NULL DEFAULT 0,
        llm_simulated   BOOLEAN     NOT NULL DEFAULT false,
        execution_ms    DOUBLE PRECISION NOT NULL DEFAULT 0,
        total_ms        DOUBLE PRECISION NOT NULL DEFAULT 0,
        detail          JSONB       NOT NULL DEFAULT '{{}}'::jsonb
    )
    """
).format(sql.Identifier(AUDIT_SCHEMA), sql.Identifier(AUDIT_TABLE))

INDEXES = (
    sql.SQL("CREATE INDEX IF NOT EXISTS ix_query_log_asked_at ON {}.{} (asked_at DESC)").format(
        sql.Identifier(AUDIT_SCHEMA), sql.Identifier(AUDIT_TABLE)
    ),
    sql.SQL("CREATE INDEX IF NOT EXISTS ix_query_log_request_id ON {}.{} (request_id)").format(
        sql.Identifier(AUDIT_SCHEMA), sql.Identifier(AUDIT_TABLE)
    ),
    sql.SQL("CREATE INDEX IF NOT EXISTS ix_query_log_status ON {}.{} (status)").format(
        sql.Identifier(AUDIT_SCHEMA), sql.Identifier(AUDIT_TABLE)
    ),
)


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One analytical request, as it will be stored.

    Deliberately flat. The log is queried with ad-hoc SQL when something has
    gone wrong, and a nested structure would mean reaching through JSON
    operators to answer "which questions abstained last week".
    """

    request_id: str
    question: str
    status: str
    conversation_id: str = ""
    intent: str = ""
    sql_text: str = ""
    tables_used: tuple[str, ...] = ()
    row_count: int = 0
    retries: int = 0
    confidence: str = ""
    validation: str = ""
    answer: str = ""
    error_code: str = ""
    model: str = ""
    llm_calls: int = 0
    llm_simulated: bool = False
    execution_ms: float = 0.0
    total_ms: float = 0.0
    #: Anything structured worth keeping but not worth a column: the plan, the
    #: confidence signals, the investigation's steps.
    detail: dict[str, Any] = field(default_factory=dict)
    asked_at: dt.datetime | None = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n-- truncated at {limit} characters --"


def ensure_audit_schema(settings: Settings | None = None) -> None:
    """Create the audit schema, table and grants. Idempotent.

    Runs as superuser, from seeding — not from the application, which must not
    hold the rights to create anything.
    """
    settings = settings or get_settings()
    audit_user = settings.postgres_audit_user
    readonly_user = settings.postgres_readonly_user

    with psycopg.connect(to_libpq(settings.superuser_dsn), autocommit=True) as conn:
        conn.execute(CREATE_SCHEMA)
        conn.execute(CREATE_TABLE)
        for index in INDEXES:
            conn.execute(index)

        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(AUDIT_SCHEMA), sql.Identifier(audit_user)
            )
        )
        # INSERT only. The writer cannot read the log back, cannot update a
        # row it already wrote, and cannot delete one — which is what makes
        # the trail worth having.
        conn.execute(
            sql.SQL("GRANT INSERT ON {}.{} TO {}").format(
                sql.Identifier(AUDIT_SCHEMA),
                sql.Identifier(AUDIT_TABLE),
                sql.Identifier(audit_user),
            )
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
                sql.Identifier(AUDIT_SCHEMA), sql.Identifier(audit_user)
            )
        )

        # The read-only role must not reach the audit schema. Without this it
        # inherits nothing by default, but stating it makes the boundary
        # explicit and survives someone later granting on ALL SCHEMAS.
        conn.execute(
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(
                sql.Identifier(AUDIT_SCHEMA), sql.Identifier(readonly_user)
            )
        )
        conn.execute(
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(
                sql.Identifier(AUDIT_SCHEMA), sql.Identifier(readonly_user)
            )
        )
        conn.execute(
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(AUDIT_SCHEMA))
        )

    logger.info("audit schema ready; INSERT granted to %s", audit_user)


_INSERT = sql.SQL(
    """
    INSERT INTO {}.{} (
        request_id, conversation_id, question, intent, status, sql_text,
        tables_used, row_count, retries, confidence, validation, answer,
        error_code, model, llm_calls, llm_simulated, execution_ms, total_ms,
        detail
    ) VALUES (
        %(request_id)s, %(conversation_id)s, %(question)s, %(intent)s,
        %(status)s, %(sql_text)s, %(tables_used)s, %(row_count)s, %(retries)s,
        %(confidence)s, %(validation)s, %(answer)s, %(error_code)s, %(model)s,
        %(llm_calls)s, %(llm_simulated)s, %(execution_ms)s, %(total_ms)s,
        %(detail)s
    )
    """
).format(sql.Identifier(AUDIT_SCHEMA), sql.Identifier(AUDIT_TABLE))


def record(entry: AuditEntry, settings: Settings | None = None) -> bool:
    """Write one entry. Returns whether it landed.

    Never raises. See the module docstring: an answered question must not turn
    into an error because the log was unreachable.
    """
    settings = settings or get_settings()
    try:
        with psycopg.connect(to_libpq(settings.audit_dsn), connect_timeout=5) as conn:
            conn.execute(
                _INSERT,
                {
                    "request_id": entry.request_id,
                    "conversation_id": entry.conversation_id,
                    "question": _truncate(entry.question, MAX_QUESTION_LENGTH),
                    "intent": entry.intent,
                    "status": entry.status,
                    "sql_text": _truncate(entry.sql_text, MAX_SQL_LENGTH),
                    "tables_used": list(entry.tables_used),
                    "row_count": entry.row_count,
                    "retries": entry.retries,
                    "confidence": entry.confidence,
                    "validation": entry.validation,
                    "answer": _truncate(entry.answer, MAX_QUESTION_LENGTH),
                    "error_code": entry.error_code,
                    "model": entry.model,
                    "llm_calls": entry.llm_calls,
                    "llm_simulated": entry.llm_simulated,
                    "execution_ms": entry.execution_ms,
                    "total_ms": entry.total_ms,
                    "detail": json.dumps(entry.detail, default=str),
                },
            )
        return True
    except (psycopg.Error, OSError) as error:
        logger.warning("audit_write_failed request_id=%s: %s", entry.request_id, str(error)[:200])
        return False
