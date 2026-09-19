"""CORS must be an explicit allow-list, never a wildcard."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

ALLOWED = "http://localhost:5173"
DISALLOWED = "http://evil.example"


def _client() -> TestClient:
    return TestClient(create_app(Settings(cors_allowed_origins=[ALLOWED])))


def test_allowed_origin_receives_cors_headers():
    response = _client().get("/api/health", headers={"Origin": ALLOWED})
    assert response.headers.get("access-control-allow-origin") == ALLOWED


def test_disallowed_origin_is_not_granted_access():
    response = _client().get("/api/health", headers={"Origin": DISALLOWED})
    assert response.headers.get("access-control-allow-origin") != DISALLOWED


def test_no_wildcard_origin_is_ever_returned():
    """Wildcard + credentials is the classic misconfiguration."""
    response = _client().get("/api/health", headers={"Origin": ALLOWED})
    assert response.headers.get("access-control-allow-origin") != "*"


def test_preflight_rejects_a_mutating_method():
    """Only GET/POST/OPTIONS are exposed; the API has no PUT/DELETE surface."""
    response = _client().options(
        "/api/health",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "DELETE",
        },
    )
    allowed = response.headers.get("access-control-allow-methods", "")
    assert "DELETE" not in allowed


def test_preflight_allows_get():
    response = _client().options(
        "/api/health",
        headers={"Origin": ALLOWED, "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 200
    assert "GET" in response.headers.get("access-control-allow-methods", "")
