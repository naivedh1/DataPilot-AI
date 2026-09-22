"""API endpoint tests.

The agent is stubbed so these test the HTTP boundary — validation, status codes,
response shape, conversation wiring — rather than re-testing the graph.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agents.runner import AgentRun
from app.api.routes import query as query_routes
from app.core.config import AppEnv, Settings
from app.main import create_app
from app.models import TABLE_LOAD_ORDER
from app.services.conversations import ConversationStore, get_store


def _run(**overrides: Any) -> AgentRun:
    defaults: dict[str, Any] = {
        "request_id": "req-1",
        "question": "revenue by region",
        "status": "success",
        "answer": "EMEA led with 1,234.00.",
        "sql": "SELECT region, revenue FROM orders",
        "columns": ["region", "revenue"],
        "rows": [["EMEA", 1234.0], ["APAC", 900.0]],
        "row_count": 2,
        "chart": {
            "chart_type": "bar",
            "title": "Revenue by region",
            "x_label": "Region",
            "y_label": "Revenue",
            "x_values": ["EMEA", "APAC"],
            "series": [{"name": "Revenue", "values": [1234.0, 900.0]}],
            "rationale": "categories",
            "value_format": "currency",
        },
        "insights": [{"text": "EMEA leads.", "kind": "finding", "supporting_values": ["1,234.00"]}],
        "tables_used": ["orders", "regions"],
        "intent": "aggregation",
        "traces": [{"node": "planner", "duration_ms": 1.0, "status": "ok", "detail": ""}],
        "attempts": [
            {"sql": "SELECT 1", "valid": True, "executed": True, "error": "", "stage": "execution"}
        ],
        "total_ms": 42.0,
        "execution_ms": 12.0,
        "llm_calls": 3,
        "llm_simulated": True,
    }
    return AgentRun(**{**defaults, **overrides})


@pytest.fixture
def store() -> ConversationStore:
    fresh = ConversationStore()
    return fresh


@pytest.fixture
def client(store, monkeypatch) -> TestClient:
    """A client whose agent is stubbed and whose store is isolated."""
    monkeypatch.setattr(query_routes, "run_agent", lambda *a, **k: _run())
    app = create_app(Settings(app_env=AppEnv.TEST))
    app.dependency_overrides[get_store] = lambda: store
    return TestClient(app)


class TestQueryEndpoint:
    def test_returns_the_full_answer_shape(self, client):
        response = client.post("/api/query", json={"question": "revenue by region"})
        assert response.status_code == 200

        body = response.json()
        for key in (
            "request_id",
            "conversation_id",
            "answer",
            "sql",
            "columns",
            "rows",
            "chart",
            "insights",
            "execution",
            "trace",
            "attempts",
        ):
            assert key in body, key

    def test_execution_metadata_is_reported(self, client):
        body = client.post("/api/query", json={"question": "revenue"}).json()
        execution = body["execution"]
        assert execution["status"] == "success"
        assert execution["row_count"] == 2
        assert execution["total_ms"] > 0
        assert execution["tables_used"] == ["orders", "regions"]

    def test_simulated_answers_are_flagged(self, client):
        """A user must be able to tell a model answer from the offline baseline."""
        body = client.post("/api/query", json={"question": "revenue"}).json()
        assert body["execution"]["llm_simulated"] is True

    def test_the_sql_is_returned_as_evidence(self, client):
        body = client.post("/api/query", json={"question": "revenue"}).json()
        assert "SELECT" in body["sql"]

    def test_every_attempt_is_returned_including_rejected_ones(self, client, monkeypatch):
        monkeypatch.setattr(
            query_routes,
            "run_agent",
            lambda *a, **k: _run(
                attempts=[
                    {
                        "sql": "DELETE FROM orders",
                        "valid": False,
                        "executed": False,
                        "error": "DELETE is not permitted",
                        "stage": "validation",
                    },
                    {
                        "sql": "SELECT 1",
                        "valid": True,
                        "executed": True,
                        "error": "",
                        "stage": "execution",
                    },
                ]
            ),
        )
        body = client.post("/api/query", json={"question": "revenue"}).json()
        assert len(body["attempts"]) == 2
        assert body["attempts"][0]["valid"] is False

    def test_a_failed_run_is_200_not_500(self, client, monkeypatch):
        """The request succeeded; the analysis did not. A 500 would make a
        declined question indistinguishable from a crashed server, and would
        discard the evidence the client needs."""
        monkeypatch.setattr(
            query_routes,
            "run_agent",
            lambda *a, **k: _run(
                status="error",
                answer="",
                error_message="The query could not be completed.",
            ),
        )
        response = client.post("/api/query", json={"question": "revenue"})
        assert response.status_code == 200
        assert response.json()["execution"]["status"] == "error"
        assert response.json()["error"]


class TestInputValidation:
    @pytest.mark.parametrize("question", ["", "  ", "ab"])
    def test_too_short_is_rejected(self, client, question):
        assert client.post("/api/query", json={"question": question}).status_code == 422

    def test_overlong_input_is_rejected(self, client):
        """Bounds the token spend a single request can trigger."""
        response = client.post("/api/query", json={"question": "x" * 5000})
        assert response.status_code == 422

    def test_missing_field_is_rejected(self, client):
        assert client.post("/api/query", json={}).status_code == 422

    def test_whitespace_is_stripped(self, client):
        response = client.post("/api/query", json={"question": "   revenue by region   "})
        assert response.status_code == 200


class TestConversations:
    def test_a_conversation_is_created_automatically(self, client):
        body = client.post("/api/query", json={"question": "revenue"}).json()
        assert body["conversation_id"]

    def test_a_follow_up_reuses_the_conversation(self, client):
        first = client.post("/api/query", json={"question": "revenue"}).json()
        second = client.post(
            "/api/query",
            json={"question": "only last month", "conversation_id": first["conversation_id"]},
        ).json()
        assert second["conversation_id"] == first["conversation_id"]

    def test_history_is_passed_to_the_agent(self, client, store, monkeypatch):
        seen: list[int] = []

        def capture(question: str, **kwargs: Any) -> AgentRun:
            seen.append(len(kwargs.get("history") or []))
            return _run()

        monkeypatch.setattr(query_routes, "run_agent", capture)

        first = client.post("/api/query", json={"question": "revenue"}).json()
        client.post(
            "/api/query",
            json={"question": "follow up", "conversation_id": first["conversation_id"]},
        )
        assert seen == [0, 1]

    def test_an_unknown_conversation_id_starts_a_new_one(self, client):
        body = client.post(
            "/api/query", json={"question": "revenue", "conversation_id": "does-not-exist"}
        ).json()
        assert body["conversation_id"] != "does-not-exist"

    def test_explicit_conversation_creation(self, client):
        response = client.post("/api/conversations")
        assert response.status_code == 201
        assert response.json()["conversation_id"]


class TestHistoryEndpoint:
    def test_empty_history(self, client):
        body = client.get("/api/history").json()
        assert body["conversations"] == []
        assert body["queries"] == []

    def test_queries_are_listed_after_running(self, client):
        client.post("/api/query", json={"question": "revenue by region"})
        body = client.get("/api/history").json()
        assert len(body["queries"]) == 1
        assert body["queries"][0]["question"] == "revenue by region"

    def test_limit_is_enforced(self, client):
        assert client.get("/api/history?limit=0").status_code == 422
        assert client.get("/api/history?limit=500").status_code == 422


class TestQueryRetrieval:
    def test_a_stored_query_can_be_fetched(self, client):
        created = client.post("/api/query", json={"question": "revenue"}).json()
        fetched = client.get(f"/api/query/{created['request_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["question"] == "revenue by region"

    def test_unknown_id_is_404(self, client):
        assert client.get("/api/query/nope").status_code == 404


class TestSchemaEndpoint:
    def test_all_tables_are_described(self, client):
        body = client.get("/api/schema").json()
        names = {table["name"] for table in body["tables"]}
        assert names == set(TABLE_LOAD_ORDER)

    def test_allowed_values_are_exposed(self, client):
        """So a user can see which literals are valid, and so can the model."""
        body = client.get("/api/schema").json()
        orders = next(t for t in body["tables"] if t["name"] == "orders")
        status = next(c for c in orders["columns"] if c["name"] == "status")
        assert set(status["allowed_values"]) == {"completed", "pending", "cancelled", "returned"}

    def test_metric_definitions_are_exposed(self, client):
        body = client.get("/api/schema").json()
        names = {metric["name"] for metric in body["metrics"]}
        assert "revenue" in names
        revenue = next(m for m in body["metrics"] if m["name"] == "revenue")
        assert "completed" in revenue["expression"]


class TestSuggestions:
    def test_suggestions_are_returned(self, client):
        body = client.get("/api/suggestions").json()
        assert len(body["suggestions"]) >= 5

    def test_suggestions_are_questions_not_answers(self, client):
        """They are inputs to the same agent path as anything typed. Nothing
        here is pre-computed."""
        for suggestion in client.get("/api/suggestions").json()["suggestions"]:
            assert "?" in suggestion or suggestion[0].isupper()


class TestCorrelation:
    def test_request_id_is_echoed(self, client):
        response = client.post(
            "/api/query",
            json={"question": "revenue"},
            headers={"X-Request-ID": "trace-123"},
        )
        assert response.headers["X-Request-ID"] == "trace-123"

    def test_a_request_id_is_generated_when_absent(self, client):
        response = client.post("/api/query", json={"question": "revenue"})
        assert response.headers.get("X-Request-ID")


class TestErrorDisclosure:
    def test_an_unexpected_error_is_not_echoed(self, client, monkeypatch):
        """A traceback can carry schema names, file paths and credentials."""

        def explode(*_a: Any, **_k: Any) -> AgentRun:
            raise RuntimeError("connection to postgresql://u:hunter2@db failed")

        monkeypatch.setattr(query_routes, "run_agent", explode)
        # raise_server_exceptions=False so the app's own handler produces the
        # response, which is what a real client would receive. The default
        # re-raises into the test and bypasses the handler entirely.
        app = client.app
        with TestClient(app, raise_server_exceptions=False) as safe_client:
            response = safe_client.post("/api/query", json={"question": "revenue"})
        assert response.status_code == 500
        body = response.text
        assert "hunter2" not in body
        assert "postgresql://" not in body
        assert "Traceback" not in body
