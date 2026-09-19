"""Typed exception hierarchy.

Every failure the user can trigger maps to a `DataPilotError` subclass carrying
a stable machine-readable `code` and a `safe_message` that is explicitly cleared
for external display. Raw driver errors and tracebacks never reach the client:
the API layer logs the original and returns only `safe_message`.
"""

from __future__ import annotations

from http import HTTPStatus


class DataPilotError(Exception):
    """Base class for all application errors."""

    code: str = "internal_error"
    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    safe_message: str = "An unexpected error occurred."

    def __init__(self, detail: str | None = None, *, safe_message: str | None = None) -> None:
        #: Full technical detail — logged server-side, never returned to a client.
        self.detail = detail or self.safe_message
        if safe_message is not None:
            self.safe_message = safe_message
        super().__init__(self.detail)


class ConfigurationError(DataPilotError):
    """Required configuration is missing or invalid."""

    code = "configuration_error"
    safe_message = "The service is not configured correctly."


class LLMError(DataPilotError):
    """The language model call failed, timed out, or returned unusable output."""

    code = "llm_error"
    status_code = HTTPStatus.BAD_GATEWAY
    safe_message = "The AI model could not complete this request."


class SchemaRetrievalError(DataPilotError):
    """Relevant schema for the question could not be resolved."""

    code = "schema_retrieval_error"
    safe_message = "Could not determine which tables are relevant to this question."


class SQLValidationError(DataPilotError):
    """Generated SQL failed a safety or correctness check and was not executed."""

    code = "sql_validation_error"
    status_code = HTTPStatus.BAD_REQUEST
    safe_message = "The generated query was rejected by the safety validator."


class SQLExecutionError(DataPilotError):
    """The database rejected or aborted the query."""

    code = "sql_execution_error"
    status_code = HTTPStatus.BAD_REQUEST
    safe_message = "The query could not be executed against the database."


class QueryTimeoutError(SQLExecutionError):
    """The query exceeded the configured statement timeout."""

    code = "query_timeout"
    status_code = HTTPStatus.GATEWAY_TIMEOUT
    safe_message = "The query took too long and was cancelled."


class AgentLimitExceededError(DataPilotError):
    """The agent graph hit its step or retry ceiling without producing an answer."""

    code = "agent_limit_exceeded"
    safe_message = "The analysis could not be completed within the allowed steps."


class QueryNotFoundError(DataPilotError):
    """A query id was requested that does not exist."""

    code = "query_not_found"
    status_code = HTTPStatus.NOT_FOUND
    safe_message = "No query was found with that id."
