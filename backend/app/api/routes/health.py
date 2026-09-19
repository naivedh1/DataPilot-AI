"""Liveness and readiness reporting."""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.core.config import Settings, get_settings
from app.schemas.health import HealthResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="Service health")
async def health() -> HealthResponse:
    """Report service status and which optional dependencies are configured.

    Deliberately reports *configuration* presence rather than performing live
    dependency checks, so that the endpoint stays fast and never leaks
    credential material. Deep checks belong on a separate readiness probe.
    """
    settings: Settings = get_settings()
    return HealthResponse(
        status="ok",
        version=__version__,
        environment=settings.app_env.value,
        llm_configured=settings.llm_configured,
        database_configured=bool(settings.postgres_readonly_password.get_secret_value()),
    )
