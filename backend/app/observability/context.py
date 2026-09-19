"""Request-scoped correlation context.

A `ContextVar` rather than a parameter threaded through every call: the agent
graph is many layers deep, and passing a request id through each of them would
add a parameter to code that has no other reason to know about HTTP.

ContextVars are safe here because FastAPI runs each request in its own context —
including sync endpoints dispatched to the thread pool.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="")
_conversation_id: ContextVar[str] = ContextVar("conversation_id", default="")


def current_request_id() -> str:
    return _request_id.get()


def current_conversation_id() -> str:
    return _conversation_id.get()


@contextmanager
def request_context(request_id: str, conversation_id: str = "") -> Iterator[None]:
    """Bind correlation ids for the duration of a request."""
    request_token = _request_id.set(request_id)
    conversation_token = _conversation_id.set(conversation_id)
    try:
        yield
    finally:
        _request_id.reset(request_token)
        _conversation_id.reset(conversation_token)
