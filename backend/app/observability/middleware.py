"""HTTP request logging and correlation.

One structured line per request, carrying the correlation id that every log
emitted during that request also carries — so a single query can be
reconstructed end to end from the logs alone.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.observability.context import request_context
from app.observability.logging import get_logger

logger = get_logger(__name__)

#: Endpoints whose every call would drown the log without telling anyone
#: anything. Health checks fire constantly under a load balancer.
_QUIET_PATHS = frozenset({"/api/health", "/docs", "/openapi.json", "/redoc"})


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Assign a request id, bind it to the log context, and time the request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID", "")[:64] or uuid.uuid4().hex[:12]
        started = time.perf_counter()

        with request_context(request_id):
            try:
                response = await call_next(request)
            except Exception:
                duration_ms = (time.perf_counter() - started) * 1000
                # Log and re-raise: the exception handler owns the response, but
                # an unlogged 500 is an outage nobody can diagnose.
                logger.exception(
                    "request failed",
                    method=request.method,
                    path=request.url.path,
                    duration_ms=round(duration_ms, 2),
                )
                raise

            duration_ms = (time.perf_counter() - started) * 1000
            if request.url.path not in _QUIET_PATHS:
                logger.info(
                    "request completed",
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    duration_ms=round(duration_ms, 2),
                )

            response.headers["X-Request-ID"] = request_id
            return response
