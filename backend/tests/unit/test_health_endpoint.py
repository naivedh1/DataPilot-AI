"""Tests for the health endpoint and app-factory wiring."""

from __future__ import annotations

from app.core.config import AppEnv, Settings
from app.main import create_app


def test_health_returns_ok_with_version_and_environment(client):
    response = client.get("/api/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["environment"] in {e.value for e in AppEnv}


def test_health_reports_dependency_configuration_as_booleans(client):
    body = client.get("/api/health").json()
    assert isinstance(body["llm_configured"], bool)
    assert isinstance(body["database_configured"], bool)


def test_health_response_never_contains_credential_material(client, settings):
    """A status endpoint is a classic accidental secret-leak surface."""
    raw = client.get("/api/health").text
    assert settings.gemini_api_key.get_secret_value() not in raw
    assert settings.postgres_readonly_password.get_secret_value() not in raw
    assert "password" not in raw.lower()


def test_routes_are_mounted_under_the_api_prefix(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/health").status_code == 404


def test_openapi_schema_is_generated_outside_production(client):
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/health" in schema.json()["paths"]


def test_docs_are_disabled_in_production():
    """Interactive docs are a development affordance, not a production surface."""
    prod_app = create_app(Settings(app_env=AppEnv.PRODUCTION))
    assert prod_app.docs_url is None
    assert prod_app.openapi_url is None


def test_docs_are_enabled_in_development():
    dev_app = create_app(Settings(app_env=AppEnv.DEVELOPMENT))
    assert dev_app.docs_url == "/docs"
