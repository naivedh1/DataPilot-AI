"""The guarded query executor.

Every piece of agent-generated SQL in DataPilot AI runs through
`execute_readonly()` and nowhere else. Concentrating execution in one function
means the safeguards cannot be forgotten at a call site:

* the read-only role (no write privilege exists to begin with),
* a server-side `statement_timeout`,
* a hard row cap applied while streaming, so an enormous result never lands in
  memory in the first place,
* driver errors translated into typed, non-leaking application errors.

The row cap is enforced by *fetching* at most `max_rows + 1` rows rather than by
rewriting the SQL. Injecting a `LIMIT` into a query that already has one, or one
whose outermost construct is a UNION or a CTE, is a source of subtle wrongness;
stopping the cursor is unambiguous and works for any shape of query.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.core.config import Settings, get_settings
from app.core.exceptions import QueryTimeoutError, SQLExecutionError
from app.database.session import readonly_session

logger = logging.getLogger(__name__)

#: PostgreSQL SQLSTATE codes worth distinguishing for the user.
SQLSTATE_QUERY_CANCELED = "57014"
SQLSTATE_INSUFFICIENT_PRIVILEGE = "42501"
SQLSTATE_UNDEFINED_TABLE = "42P01"
SQLSTATE_UNDEFINED_COLUMN = "42703"
SQLSTATE_UNDEFINED_FUNCTION = "42883"
SQLSTATE_SYNTAX_ERROR = "42601"
SQLSTATE_READ_ONLY_TRANSACTION = "25006"

#: SQLSTATEs whose message is safe and useful to show a user. These describe the
#: *query*, not the server, so they help the model repair its own SQL. Anything
#: else is reported generically.
REPAIRABLE_SQLSTATES = frozenset(
    {
        SQLSTATE_UNDEFINED_TABLE,
        SQLSTATE_UNDEFINED_COLUMN,
        SQLSTATE_UNDEFINED_FUNCTION,
        SQLSTATE_SYNTAX_ERROR,
    }
)


@dataclass(frozen=True, slots=True)
class QueryResult:
    """The outcome of one successful query execution."""

    sql: str
    columns: list[str]
    rows: list[tuple[Any, ...]]
    row_count: int
    duration_ms: float
    truncated: bool = False
    #: Column type names as reported by the driver, for downstream formatting.
    column_types: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.row_count == 0

    def to_records(self) -> list[dict[str, Any]]:
        """Rows as dicts, JSON-serialisable.

        Decimal becomes float and dates become ISO strings only at this
        boundary. Internally Decimal is preserved, because converting money to
        float earlier would reintroduce exactly the precision loss the NUMERIC
        columns exist to avoid.
        """
        return [
            {column: _jsonable(value) for column, value in zip(self.columns, row, strict=True)}
            for row in self.rows
        ]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _sqlstate(error: DBAPIError) -> str | None:
    """Extract the SQLSTATE from a driver error, if present."""
    original = getattr(error, "orig", None)
    sqlstate = getattr(original, "sqlstate", None)
    if sqlstate:
        return str(sqlstate)
    diagnostic = getattr(original, "diag", None)
    code = getattr(diagnostic, "sqlstate", None)
    return str(code) if code else None


def _driver_message(error: DBAPIError) -> str:
    """The database's own message, without SQLAlchemy's wrapper noise."""
    original = getattr(error, "orig", None)
    return str(original) if original else str(error)


def _translate(error: DBAPIError, sql: str) -> SQLExecutionError:
    """Turn a driver error into a typed application error.

    `detail` carries the full technical message for the log and for the SQL
    repair loop. `safe_message` is what a user sees, and is only ever the
    database's own text when that text describes the query rather than the
    server — a missing column is useful feedback; a connection failure would
    leak a DSN.
    """
    sqlstate = _sqlstate(error)
    detail = _driver_message(error)

    if sqlstate == SQLSTATE_QUERY_CANCELED:
        return QueryTimeoutError(
            detail=f"statement timeout: {detail}",
            safe_message=(
                "The query took too long and was cancelled. Try narrowing the "
                "date range or adding a filter."
            ),
        )

    if sqlstate in (SQLSTATE_INSUFFICIENT_PRIVILEGE, SQLSTATE_READ_ONLY_TRANSACTION):
        # The read-only role refused something. Worth a warning: the SQL
        # validator should have rejected this before it ever reached the driver.
        logger.warning("read-only role refused a statement that passed validation: %s", sql[:200])
        return SQLExecutionError(
            detail=detail,
            safe_message="Only read-only queries are permitted.",
        )

    if sqlstate in REPAIRABLE_SQLSTATES:
        return SQLExecutionError(detail=detail, safe_message=_first_line(detail))

    return SQLExecutionError(detail=f"[{sqlstate}] {detail}")


def _first_line(message: str) -> str:
    """The database's primary error line, without its CONTEXT/HINT tail."""
    return message.strip().splitlines()[0][:300]


def execute_readonly(
    sql: str,
    *,
    settings: Settings | None = None,
    max_rows: int | None = None,
) -> QueryResult:
    """Execute a read-only query under every configured safeguard.

    Raises `SQLExecutionError` (or `QueryTimeoutError`) on failure. The caller
    is expected to surface `safe_message` and log `detail`.
    """
    settings = settings or get_settings()
    limit = max_rows if max_rows is not None else settings.sql_max_result_rows
    started = time.perf_counter()

    try:
        with readonly_session() as session:
            cursor = session.execute(text(sql))

            # `returns_rows` is a documented SQLAlchemy attribute that the
            # stubs do not declare on the generic Result type.
            if cursor.returns_rows:  # type: ignore[attr-defined]
                # Fetch one more than the cap so truncation is detectable
                # without scanning the whole result set.
                fetched = cursor.fetchmany(limit + 1)
                truncated = len(fetched) > limit
                rows = [tuple(row) for row in fetched[:limit]]
                columns = list(cursor.keys())
            else:
                # A statement that returns nothing should not have passed
                # validation, but returning an empty result is safer than
                # assuming a cursor shape that may not exist.
                rows, columns, truncated = [], [], False

    except DBAPIError as error:
        duration_ms = (time.perf_counter() - started) * 1000
        translated = _translate(error, sql)
        logger.info("query failed after %.1fms: %s", duration_ms, translated.detail[:300])
        raise translated from error
    except SQLAlchemyError as error:
        logger.exception("unexpected database error")
        raise SQLExecutionError(detail=str(error)) from error

    duration_ms = (time.perf_counter() - started) * 1000
    if truncated:
        logger.info("result truncated to %s rows", limit)

    return QueryResult(
        sql=sql,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        duration_ms=round(duration_ms, 2),
        truncated=truncated,
    )
