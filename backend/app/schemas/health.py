"""Response models for system endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Service health and dependency-configuration summary."""

    status: Literal["ok", "degraded"] = Field(description="Overall service status.")
    version: str = Field(description="Application version.")
    environment: str = Field(description="Active deployment environment.")
    llm_configured: bool = Field(description="Whether a Gemini API key is present.")
    database_configured: bool = Field(
        description="Whether read-only database credentials are present."
    )
