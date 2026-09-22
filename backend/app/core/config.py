"""Typed application settings, loaded from environment variables / .env.

Settings are validated once at import time via `get_settings()`. Anything that
fails validation fails fast at startup rather than at the first request.

Secrets are wrapped in `SecretStr` so they cannot be leaked by an accidental
`repr()`, log line, or error traceback.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repo root is one level above `backend/`; the shared .env lives there.
REPO_ROOT = Path(__file__).resolve().parents[3]


class AppEnv(StrEnum):
    """Deployment environment. Controls docs exposure and error verbosity."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Validated configuration for the DataPilot AI backend."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", Path(".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # frontend VITE_* vars share the same file
    )

    # --- Application ------------------------------------------------------
    app_env: AppEnv = AppEnv.DEVELOPMENT
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    api_host: str = "0.0.0.0"  # noqa: S104 — containerised service binds all interfaces
    api_port: Annotated[int, Field(ge=1, le=65535)] = 8000

    # --- LLM --------------------------------------------------------------
    gemini_api_key: SecretStr = SecretStr("")
    # Verified against the live API. The 2.5 family returns 404 for keys
    # created after its retirement, so a stale default silently breaks a
    # fresh install. Pinned rather than an alias like `gemini-flash-latest`,
    # because the evaluation suite needs a model that does not shift
    # underneath it between runs.
    gemini_model: str = "gemini-3.8-flash"
    llm_timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 45
    llm_max_output_tokens: Annotated[int, Field(ge=256, le=32768)] = 4096
    llm_temperature: Annotated[float, Field(ge=0.0, le=2.0)] = 0.0

    # --- Database ---------------------------------------------------------
    postgres_host: str = "localhost"
    postgres_port: Annotated[int, Field(ge=1, le=65535)] = 5432
    postgres_db: str = "datapilot"
    postgres_readonly_user: str = "datapilot_readonly"
    postgres_readonly_password: SecretStr = SecretStr("")
    postgres_admin_user: str = "datapilot_admin"
    postgres_admin_password: SecretStr = SecretStr("")
    # Audit writer. Holds INSERT on the audit schema and nothing at all on the
    # warehouse — see `app/database/audit.py` for why it exists separately
    # rather than reusing the admin role.
    postgres_audit_user: str = "datapilot_audit"
    postgres_audit_password: SecretStr = SecretStr("")
    # Cluster superuser. Used only by scripts/seed_database.py to create the two
    # roles above and the database itself; the running application never holds
    # these credentials.
    postgres_superuser: str = "postgres"
    postgres_superuser_password: SecretStr = SecretStr("")
    #: Maintenance database to connect to when creating the target database,
    #: since CREATE DATABASE cannot run from inside the database being created.
    postgres_maintenance_db: str = "postgres"

    # --- Query safeguards -------------------------------------------------
    sql_statement_timeout_ms: Annotated[int, Field(ge=100, le=120_000)] = 10_000
    sql_max_result_rows: Annotated[int, Field(ge=1, le=100_000)] = 5_000
    sql_max_retries: Annotated[int, Field(ge=0, le=5)] = 2

    # --- Agent orchestration ----------------------------------------------
    agent_max_steps: Annotated[int, Field(ge=1, le=100)] = 25
    conversation_max_turns: Annotated[int, Field(ge=0, le=50)] = 10

    # --- CORS -------------------------------------------------------------
    # NoDecode is required: for a complex type (list), pydantic-settings runs
    # json.loads() on the raw .env string *before* any "before" validator sees
    # it, so `a.test,b.test` raises a JSONDecodeError instead of reaching the
    # splitter below. NoDecode hands the raw string over untouched.
    cors_allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma-separated string from .env as well as a real list."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    # --- Derived values ---------------------------------------------------
    def _dsn(self, user: str, password: SecretStr, database: str | None = None) -> str:
        return (
            f"postgresql+psycopg://{user}:{password.get_secret_value()}"
            f"@{self.postgres_host}:{self.postgres_port}/{database or self.postgres_db}"
        )

    # NOTE: deliberately *not* pydantic computed fields. A computed field is
    # included in repr() and model_dump(), and these strings embed the plaintext
    # password — which would defeat SecretStr entirely. Plain properties keep
    # the credential out of every serialised form of Settings.
    @property
    def readonly_dsn(self) -> str:
        """Connection string used by the API and every agent-generated query."""
        return self._dsn(self.postgres_readonly_user, self.postgres_readonly_password)

    @property
    def admin_dsn(self) -> str:
        """Connection string for migrations and seeding only. Never for user SQL."""
        return self._dsn(self.postgres_admin_user, self.postgres_admin_password)

    @property
    def audit_dsn(self) -> str:
        """Connection string for the audit writer. Never for user SQL."""
        return self._dsn(self.postgres_audit_user, self.postgres_audit_password)

    @property
    def superuser_dsn(self) -> str:
        """Superuser connection to the target database (grants, extensions)."""
        return self._dsn(self.postgres_superuser, self.postgres_superuser_password)

    @property
    def superuser_maintenance_dsn(self) -> str:
        """Superuser connection to the maintenance database.

        Needed because CREATE DATABASE and CREATE ROLE cannot be issued from
        inside the database being created.
        """
        return self._dsn(
            self.postgres_superuser,
            self.postgres_superuser_password,
            database=self.postgres_maintenance_db,
        )

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    @property
    def llm_configured(self) -> bool:
        """Whether a usable Gemini key is present. Lets the API degrade honestly."""
        key = self.gemini_api_key.get_secret_value()
        return bool(key) and not key.startswith("your-")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings singleton.

    Cached so that .env is parsed once. Tests clear the cache via
    `get_settings.cache_clear()` after monkeypatching the environment.
    """
    return Settings()


if __name__ == "__main__":
    # `python -m app.core.config` — a config smoke check that prints no secrets.
    settings = get_settings()
    print(f"app_env            : {settings.app_env}")
    print(f"api                : {settings.api_host}:{settings.api_port}")
    print(
        f"postgres           : {settings.postgres_host}:{settings.postgres_port}"
        f"/{settings.postgres_db}"
    )
    print(f"gemini model       : {settings.gemini_model}")
    print(f"gemini key present : {settings.llm_configured}")
    print(f"cors origins       : {settings.cors_allowed_origins}")
    print(f"statement timeout  : {settings.sql_statement_timeout_ms} ms")
    print(f"max result rows    : {settings.sql_max_result_rows}")
    sys.exit(0)
