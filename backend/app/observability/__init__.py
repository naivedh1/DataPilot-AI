"""Structured logging, request correlation, and secret redaction.

Emits JSON events keyed by request_id so a single query can be reconstructed
end to end. Redaction happens in the logging pipeline, not at call sites, so it
covers third-party libraries' output as well as the application's own.
"""

from app.observability.context import (
    current_conversation_id,
    current_request_id,
    request_context,
)
from app.observability.logging import configure_logging, get_logger, redact
from app.observability.middleware import RequestLoggingMiddleware

__all__ = [
    "RequestLoggingMiddleware",
    "configure_logging",
    "current_conversation_id",
    "current_request_id",
    "get_logger",
    "redact",
    "request_context",
]
