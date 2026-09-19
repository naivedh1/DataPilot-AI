"""Structured logging with secret redaction.

Logs are JSON in production and human-readable in development. Both paths run
through the same redaction processor, because the most likely way a credential
reaches a log file is a developer debugging locally and pasting the output
somewhere.

**Redaction is defence in depth, not the primary control.** Secrets are wrapped
in `SecretStr`, connection strings are never logged deliberately, and prompts
exclude credentials. This processor exists for the case where one of those slips
— a driver error embedding a DSN, an exception repr carrying a config object.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from app.core.config import AppEnv, Settings

#: Patterns that are replaced wherever they appear in a rendered log event.
#: Ordered most-specific first so a DSN is masked as a whole rather than being
#: partially caught by the password rule.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # postgresql://user:secret@host/db  ->  postgresql://user:***@host/db
    (re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)[^@\s]+(@)"), r"\1***\2"),
    # Google API keys have a recognisable shape.
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"), "***REDACTED_API_KEY***"),
    # Authorization headers. The scheme word ("Bearer", "Basic") is not the
    # secret — the token after it is. A pattern that stops at the scheme masks
    # the wrong half and leaves the credential in the log.
    (
        re.compile(
            r"\b(authorization|proxy-authorization)\s*[=:]\s*"
            r"(?:bearer\s+|basic\s+|token\s+)?[^\s'\",}&]+",
            re.IGNORECASE,
        ),
        r"\1=***",
    ),
    # key=value forms for anything credential-shaped.
    (
        re.compile(
            r"\b(password|passwd|pwd|api[_-]?key|secret|token)"
            r"\s*[=:]\s*['\"]?([^\s'\",}&]+)",
            re.IGNORECASE,
        ),
        r"\1=***",
    ),
)

#: Event keys whose value is replaced entirely, regardless of content.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "api_key",
        "gemini_api_key",
        "secret",
        "token",
        "authorization",
        "dsn",
        "connection_string",
        "postgres_password",
    }
)


def redact(value: str) -> str:
    """Mask anything credential-shaped in a string."""
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def _redact_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Redact every string value in an event, and drop sensitive keys."""
    for key, value in list(event_dict.items()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "***REDACTED***"
        elif isinstance(value, str):
            event_dict[key] = redact(value)
    return event_dict


def _add_request_id(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach the ambient request id, so one query is one searchable trace."""
    from app.observability.context import current_request_id

    request_id = current_request_id()
    if request_id and "request_id" not in event_dict:
        event_dict["request_id"] = request_id
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib root logger together.

    Both are configured because third-party libraries (SQLAlchemy, uvicorn,
    httpx) log through the stdlib. Routing those through the same processor
    chain means their output is redacted too — and a DSN in a SQLAlchemy error
    is exactly the leak worth worrying about.
    """
    level = getattr(logging, settings.log_level, logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_request_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_processor,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.app_env is AppEnv.PRODUCTION
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    # structlog hands the event dict to the stdlib formatter rather than
    # rendering it itself. Rendering in both places prints each structlog event
    # twice — once as the message, once wrapped around it.
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # `foreign_pre_chain` applies the same processors — redaction included — to
    # records from stdlib loggers such as SQLAlchemy and uvicorn.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # SQLAlchemy's INFO level echoes every statement including bound parameters.
    # Useful when debugging, far too noisy and too revealing by default.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> Any:
    """A bound structlog logger."""
    return structlog.get_logger(name)
