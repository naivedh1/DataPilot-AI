"""Behavioural tests for settings parsing, validation and secret handling."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import AppEnv, Settings


def test_cors_origins_parsed_from_comma_separated_string():
    """A .env cannot hold a JSON list, so the validator must split a string."""
    settings = Settings(cors_allowed_origins="http://a.test, http://b.test ,")
    assert settings.cors_allowed_origins == ["http://a.test", "http://b.test"]


def test_cors_origins_accepts_a_real_list():
    settings = Settings(cors_allowed_origins=["http://a.test"])
    assert settings.cors_allowed_origins == ["http://a.test"]


def test_dsn_uses_psycopg3_driver_and_readonly_role():
    settings = Settings(
        postgres_host="db.internal",
        postgres_port=6543,
        postgres_db="warehouse",
        postgres_readonly_user="ro_user",
        postgres_readonly_password=SecretStr("s3cret"),
    )
    assert settings.readonly_dsn == (
        "postgresql+psycopg://ro_user:s3cret@db.internal:6543/warehouse"
    )


def test_readonly_and_admin_dsns_use_different_roles():
    """The API role and the seeding role must never collapse into one."""
    settings = Settings(
        postgres_readonly_user="ro",
        postgres_readonly_password=SecretStr("a"),
        postgres_admin_user="admin",
        postgres_admin_password=SecretStr("b"),
    )
    assert settings.readonly_dsn != settings.admin_dsn
    assert "ro:a@" in settings.readonly_dsn
    assert "admin:b@" in settings.admin_dsn


def test_secrets_are_not_exposed_by_repr():
    """An accidental log of the settings object must not leak credentials."""
    settings = Settings(
        gemini_api_key=SecretStr("AIza-super-secret"),
        postgres_readonly_password=SecretStr("hunter2"),
        postgres_admin_password=SecretStr("admin-pw"),
    )
    dumped = repr(settings)
    assert "AIza-super-secret" not in dumped
    assert "hunter2" not in dumped
    assert "admin-pw" not in dumped


def test_secrets_are_not_exposed_by_model_dump():
    settings = Settings(
        gemini_api_key=SecretStr("AIza-super-secret"),
        postgres_readonly_password=SecretStr("hunter2"),
    )
    assert "AIza-super-secret" not in str(settings.model_dump())
    assert "hunter2" not in str(settings.model_dump())


def test_dsn_properties_are_not_serialised_into_settings():
    """Regression guard.

    Exposing the DSNs as pydantic computed fields put the plaintext password
    into repr() and model_dump(), silently defeating SecretStr. They must stay
    plain properties: reachable by attribute, absent from every dump.
    """
    settings = Settings(postgres_readonly_password=SecretStr("hunter2"))
    assert "hunter2" in settings.readonly_dsn  # still usable
    assert "readonly_dsn" not in settings.model_dump()  # but never serialised
    assert "admin_dsn" not in settings.model_dump()


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("", False),  # unset
        ("your-gemini-api-key-here", False),  # the .env.example placeholder
        ("AIzaSyRealLookingKey", True),
    ],
)
def test_llm_configured_rejects_placeholder_keys(key, expected):
    """Shipping .env.example unchanged must not look like a configured service."""
    assert Settings(gemini_api_key=SecretStr(key)).llm_configured is expected


def test_is_production_flag():
    assert Settings(app_env=AppEnv.PRODUCTION).is_production is True
    assert Settings(app_env=AppEnv.DEVELOPMENT).is_production is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_port", 0),
        ("api_port", 70_000),
        ("llm_temperature", -0.5),
        ("llm_temperature", 2.5),
        ("sql_max_result_rows", 0),
        ("sql_statement_timeout_ms", 10),  # below the 100ms floor
        ("sql_max_retries", 99),  # above the retry ceiling
        ("agent_max_steps", 0),
        ("log_level", "TRACE"),  # not a real level
        ("app_env", "staging"),  # not a known environment
    ],
)
def test_out_of_range_settings_are_rejected(field, value):
    """Bad configuration must fail at startup, not at the first request."""
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_retry_ceiling_allows_zero_for_deterministic_evaluation():
    """Evaluation runs disable repair to measure first-attempt SQL quality."""
    assert Settings(sql_max_retries=0).sql_max_retries == 0


# ---------------------------------------------------------------------------
# Loading from a real .env file.
#
# These exercise a genuinely different code path from Settings(**kwargs): the
# dotenv source decodes complex types itself before any validator runs. A
# comma-separated CORS list parsed fine via kwargs but raised a JSONDecodeError
# via .env until the field was annotated NoDecode. Tests that only construct
# Settings directly cannot catch that class of bug.
# ---------------------------------------------------------------------------


#: Settings keys that pydantic-settings reads from the real process environment.
#: Those take precedence over an `_env_file`, by design — so a dotenv test must
#: clear them first or it silently asserts against whatever the developer
#: happens to have exported, and passes or fails by machine.
_ENV_KEYS = (
    "APP_ENV",
    "LOG_LEVEL",
    "API_HOST",
    "API_PORT",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_READONLY_USER",
    "POSTGRES_READONLY_PASSWORD",
    "POSTGRES_ADMIN_USER",
    "POSTGRES_ADMIN_PASSWORD",
    "POSTGRES_SUPERUSER",
    "POSTGRES_SUPERUSER_PASSWORD",
    "SQL_STATEMENT_TIMEOUT_MS",
    "SQL_MAX_RESULT_ROWS",
    "SQL_MAX_RETRIES",
    "AGENT_MAX_STEPS",
    "CONVERSATION_MAX_TURNS",
    "CORS_ALLOWED_ORIGINS",
)


@pytest.fixture
def isolated_env(monkeypatch):
    """Remove DataPilot settings from the process environment.

    Without this, these tests assert against the developer's exported variables
    rather than the file under test — passing locally and failing in CI, or the
    reverse.
    """
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_env(tmp_path, body: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(body, encoding="utf-8")
    return env_file


def test_settings_load_from_a_dotenv_file(tmp_path, isolated_env):
    env_file = _write_env(
        tmp_path,
        "APP_ENV=production\nAPI_PORT=9001\nGEMINI_MODEL=gemini-2.5-pro\nPOSTGRES_DB=warehouse\n",
    )
    settings = Settings(_env_file=env_file)
    assert settings.app_env is AppEnv.PRODUCTION
    assert settings.api_port == 9001
    assert settings.gemini_model == "gemini-2.5-pro"
    assert settings.postgres_db == "warehouse"


def test_comma_separated_cors_origins_parse_from_a_dotenv_file(tmp_path, isolated_env):
    """Regression guard for the NoDecode fix.

    A .env cannot express a JSON list, and pydantic-settings would otherwise
    try json.loads() on this value before the splitting validator ran.
    """
    env_file = _write_env(
        tmp_path,
        "CORS_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173\n",
    )
    settings = Settings(_env_file=env_file)
    assert settings.cors_allowed_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_single_cors_origin_parses_from_a_dotenv_file(tmp_path, isolated_env):
    env_file = _write_env(tmp_path, "CORS_ALLOWED_ORIGINS=https://app.example.com\n")
    assert Settings(_env_file=env_file).cors_allowed_origins == ["https://app.example.com"]


def test_secrets_load_from_a_dotenv_file_and_stay_wrapped(tmp_path, isolated_env):
    env_file = _write_env(
        tmp_path,
        "GEMINI_API_KEY=AIza-from-dotenv\nPOSTGRES_READONLY_PASSWORD=ro-from-dotenv\n",
    )
    settings = Settings(_env_file=env_file)
    assert settings.gemini_api_key.get_secret_value() == "AIza-from-dotenv"
    assert settings.llm_configured is True
    assert "AIza-from-dotenv" not in repr(settings)
    assert "ro-from-dotenv" not in repr(settings)


def test_unknown_keys_in_dotenv_are_ignored(tmp_path, isolated_env):
    """The frontend's VITE_* vars share the same file and must not break startup."""
    env_file = _write_env(
        tmp_path,
        "VITE_API_BASE_URL=http://localhost:8000\nSOME_FUTURE_FLAG=1\nAPI_PORT=8080\n",
    )
    assert Settings(_env_file=env_file).api_port == 8080


def test_invalid_value_in_dotenv_fails_loudly(tmp_path, isolated_env):
    """Misconfiguration must surface at startup, not as a silent default."""
    env_file = _write_env(tmp_path, "API_PORT=not-a-number\n")
    with pytest.raises(ValidationError):
        Settings(_env_file=env_file)
