"""FastAPI application entry point.

Run locally from `backend/`:

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import health, query, schema
from app.core.config import Settings, get_settings
from app.core.exceptions import DataPilotError
from app.database.session import dispose_engines
from app.observability import RequestLoggingMiddleware, configure_logging, get_logger

API_PREFIX = "/api"

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Start-up and shut-down.

    Engines are disposed on shutdown so pooled connections are closed cleanly
    rather than left for the server to time out.
    """
    settings: Settings = app.state.settings
    logger.info(
        "starting",
        version=__version__,
        environment=settings.app_env.value,
        llm_configured=settings.llm_configured,
    )
    if not settings.llm_configured:
        logger.warning(
            "no Gemini API key configured; the deterministic offline baseline "
            "will be used and every response will be marked as simulated"
        )
    yield
    dispose_engines()
    logger.info("stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Accepting an optional `settings` makes the app constructible with a test
    configuration without touching process-wide environment state.
    """
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="DataPilot AI",
        description=(
            "An agentic AI data analyst. Turns natural-language questions into "
            "validated, read-only SQL, then analyses and explains the results."
        ),
        version=__version__,
        lifespan=lifespan,
        # Interactive docs are a development affordance, not a production surface.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings

    app.add_middleware(RequestLoggingMiddleware)

    # Explicit origin allow-list. No wildcard: credentials may be sent later.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    @app.exception_handler(DataPilotError)
    async def handle_application_error(_request: Request, error: DataPilotError) -> JSONResponse:
        """Return the safe message; log the technical detail.

        This split is the whole point of the typed error hierarchy: `detail` can
        name a table, a role or a connection string, and must never reach a
        client.
        """
        logger.warning("application error", code=error.code, detail=error.detail)
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": error.safe_message}},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        """Last resort. Never echo an unexpected exception to the client.

        A traceback or a driver message can carry schema names, file paths and
        credentials.
        """
        logger.exception("unhandled error", error_type=type(error).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred.",
                }
            },
        )

    api = APIRouter(prefix=API_PREFIX)
    api.include_router(health.router)
    api.include_router(query.router)
    api.include_router(schema.router)
    app.include_router(api)

    return app


app = create_app()
