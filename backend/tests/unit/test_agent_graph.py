"""Agent graph tests.

The retry edges are the point of using a graph at all, so most of this file is
about them: that a failure routes back to generation with the reason attached,
that the budget is respected, and that no path can loop forever.

Providers are stubbed in-process, so nothing here touches a network. Nodes that
run SQL are stubbed too where the test is about routing rather than execution.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from app.agents import graph as graph_module
from app.agents import nodes as nodes_module
from app.agents.runner import run_agent
from app.agents.state import ConversationTurn, new_state
from app.core.config import AppEnv, Settings
from app.database.executor import QueryResult
from app.schemas.agent import (
    AnalysisIntent,
    InsightBundle,
    InsightItem,
    PlannerDecision,
    SQLGeneration,
)
from app.services.llm.base import LLMProvider, LLMResponse, LLMUsage


class ScriptedProvider(LLMProvider):
    """Returns a queued response per schema type, recording every call.

    Scripted rather than random so a test can assert exactly what the graph did:
    how many times generation was invoked, and with what error text.
    """

    name = "scripted"

    def __init__(self, **queues: list[BaseModel]) -> None:
        self.queues = {key: list(value) for key, value in queues.items()}
        self.calls: list[tuple[str, str]] = []

    @property
    def is_live(self) -> bool:
        return False

    def complete(self, *, system: str, prompt: str, **_: Any) -> LLMResponse:
        self.calls.append(("text", prompt))
        return LLMResponse(text="ok", usage=LLMUsage(simulated=True))

    def complete_json(
        self, *, system: str, prompt: str, schema: type[BaseModel], **_: Any
    ) -> tuple[Any, LLMUsage]:
        self.calls.append((schema.__name__, prompt))
        queue = self.queues.get(schema.__name__)
        if not queue:
            raise AssertionError(f"no scripted response for {schema.__name__}")
        # The last response repeats, so a test need only script what it asserts.
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item, LLMUsage(simulated=True)


def _plan(**overrides: Any) -> PlannerDecision:
    defaults: dict[str, Any] = {
        "intent": AnalysisIntent.AGGREGATION,
        "needs_sql": True,
        "needs_analysis": False,
        "needs_chart": True,
        "resolved_question": "revenue by region",
        "time_grain": "none",
    }
    return PlannerDecision(**{**defaults, **overrides})


def _sql(sql: str, **overrides: Any) -> SQLGeneration:
    return SQLGeneration(sql=sql, tables_used=["orders"], metrics=["revenue"], **overrides)


def _insight() -> InsightBundle:
    return InsightBundle(
        headline="Revenue totalled 1,234.00.",
        insights=[InsightItem(text="A finding.", kind="finding")],
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(app_env=AppEnv.TEST, sql_max_retries=2, agent_max_steps=25)


@pytest.fixture
def stub_execution(monkeypatch):
    """Replace SQL execution with a recorded stub.

    Returns the call log so a test can assert which SQL actually reached the
    database — which is how "did the retry use the corrected query?" is checked.
    """
    executed: list[str] = []

    def fake_execute(sql: str, **_: Any) -> QueryResult:
        executed.append(sql)
        return QueryResult(
            sql=sql,
            columns=["region", "revenue"],
            rows=[("EMEA", 1234.0), ("APAC", 900.0), ("NA", 2000.0)],
            row_count=3,
            duration_ms=5.0,
        )

    monkeypatch.setattr(nodes_module, "execute_readonly", fake_execute)
    return executed


def _install(monkeypatch, provider: ScriptedProvider) -> None:
    monkeypatch.setattr(nodes_module, "get_provider", lambda: provider)


class TestHappyPath:
    def test_all_nodes_run_in_order(self, monkeypatch, settings, stub_execution):
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[_sql("SELECT 1 AS revenue FROM orders")],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run = run_agent("revenue by region", settings=settings)

        assert run.status == "success"
        assert [trace["node"] for trace in run.traces] == [
            "planner",
            "schema_retrieval",
            "sql_generation",
            "sql_validation",
            "sql_execution",
            "analysis",
            "visualization",
            "insight",
            "response",
        ]

    def test_result_carries_the_evidence(self, monkeypatch, settings, stub_execution):
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[_sql("SELECT 1 AS revenue FROM orders")],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run = run_agent("revenue by region", settings=settings)

        assert run.sql
        assert run.columns == ["region", "revenue"]
        assert run.row_count == 3
        assert run.chart is not None
        assert run.insights
        assert run.answer == "Revenue totalled 1,234.00."
        assert run.retries == 0

    def test_no_retry_on_a_clean_run(self, monkeypatch, settings, stub_execution):
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[_sql("SELECT 1 AS revenue FROM orders")],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run_agent("revenue by region", settings=settings)
        generations = [call for call in provider.calls if call[0] == "SQLGeneration"]
        assert len(generations) == 1


class TestValidationRetry:
    def test_invalid_sql_is_regenerated(self, monkeypatch, settings, stub_execution):
        """The first attempt is a write; the validator must reject it and the
        graph must route back to generation rather than to the database."""
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[
                _sql("DELETE FROM orders"),
                _sql("SELECT 1 AS revenue FROM orders"),
            ],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run = run_agent("revenue by region", settings=settings)

        assert run.status == "success"
        assert run.retries == 1
        assert len(stub_execution) == 1, "the rejected SQL must never execute"
        assert "DELETE" not in stub_execution[0].upper()

    def test_the_rejection_reason_reaches_the_retry_prompt(
        self, monkeypatch, settings, stub_execution
    ):
        """A retry without the reason is a blind re-roll."""
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[
                _sql("SELECT * FROM nonexistent_table"),
                _sql("SELECT 1 AS revenue FROM orders"),
            ],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run_agent("revenue by region", settings=settings)

        generations = [prompt for name, prompt in provider.calls if name == "SQLGeneration"]
        assert len(generations) == 2
        assert "PREVIOUS ATTEMPT FAILED" in generations[1]
        assert "nonexistent_table" in generations[1]

    def test_every_attempt_is_recorded(self, monkeypatch, settings, stub_execution):
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[
                _sql("DROP TABLE orders"),
                _sql("SELECT 1 AS revenue FROM orders"),
            ],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run = run_agent("revenue by region", settings=settings)

        assert len(run.attempts) >= 2
        assert run.attempts[0]["valid"] is False
        assert not run.attempts[0]["executed"]


class TestExecutionRetry:
    def test_a_database_error_triggers_regeneration(self, monkeypatch, settings):
        """The database's own message is the best possible repair signal."""
        from app.core.exceptions import SQLExecutionError

        calls: list[str] = []

        def flaky(sql: str, **_: Any) -> QueryResult:
            calls.append(sql)
            if len(calls) == 1:
                raise SQLExecutionError(
                    detail='column "revenu" does not exist',
                    safe_message='column "revenu" does not exist',
                )
            return QueryResult(
                sql=sql, columns=["revenue"], rows=[(1.0,)], row_count=1, duration_ms=1.0
            )

        monkeypatch.setattr(nodes_module, "execute_readonly", flaky)
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[
                _sql("SELECT revenu FROM orders"),
                _sql("SELECT revenue FROM orders"),
            ],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run = run_agent("revenue", settings=settings)

        assert run.status == "success"
        assert run.retries == 1
        assert len(calls) == 2

        generations = [p for name, p in provider.calls if name == "SQLGeneration"]
        assert "revenu" in generations[1]


class TestRetryBudget:
    def test_persistent_failure_terminates(self, monkeypatch, settings, stub_execution):
        """The critical property: a model that keeps producing invalid SQL must
        stop, not loop."""
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[_sql("DELETE FROM orders")],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run = run_agent("revenue by region", settings=settings)

        assert run.status == "error"
        assert run.retries == settings.sql_max_retries
        assert stub_execution == [], "no invalid SQL may reach the database"

    def test_generation_is_called_exactly_budget_plus_one_times(
        self, monkeypatch, settings, stub_execution
    ):
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[_sql("DROP TABLE orders")],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run_agent("revenue", settings=settings)

        generations = [c for c in provider.calls if c[0] == "SQLGeneration"]
        assert len(generations) == settings.sql_max_retries + 1

    def test_zero_retries_gives_a_single_attempt(self, monkeypatch, stub_execution):
        """Evaluation runs with the budget at zero to measure first-attempt
        quality, so this configuration must be honoured exactly."""
        strict = Settings(app_env=AppEnv.TEST, sql_max_retries=0)
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[_sql("DELETE FROM orders")],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run = run_agent("revenue", settings=strict)

        assert run.status == "error"
        assert run.retries == 0
        assert len([c for c in provider.calls if c[0] == "SQLGeneration"]) == 1

    def test_failure_message_is_safe(self, monkeypatch, settings, stub_execution):
        """A terminal error must not become an information-disclosure channel."""
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[_sql("DELETE FROM orders")],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run = run_agent("revenue", settings=settings)

        lowered = run.error_message.lower()
        assert "postgresql://" not in lowered
        assert "password" not in lowered


class TestShortCircuits:
    def test_an_unsupported_question_skips_sql_entirely(self, monkeypatch, settings):
        provider = ScriptedProvider(
            PlannerDecision=[
                _plan(
                    intent=AnalysisIntent.UNSUPPORTED,
                    needs_sql=False,
                    unsupported_reason="The warehouse holds no marketing spend.",
                )
            ],
        )
        _install(monkeypatch, provider)

        run = run_agent("what did we spend on ads?", settings=settings)

        assert run.status == "no_query"
        assert "marketing spend" in run.error_message
        nodes = [trace["node"] for trace in run.traces]
        assert "sql_execution" not in nodes

    def test_a_declined_generation_terminates_cleanly(self, monkeypatch, settings, stub_execution):
        """An empty sql string is an honest refusal, not a failure to retry."""
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[SQLGeneration(sql="", reasoning_summary="No column holds ad spend.")],
        )
        _install(monkeypatch, provider)

        run = run_agent("ad spend by month", settings=settings)

        assert run.status == "no_query"
        assert "ad spend" in run.error_message
        assert stub_execution == []

    def test_an_empty_result_produces_an_honest_answer(self, monkeypatch, settings):
        """Asking a model to comment on zero rows invites invention."""

        def empty(sql: str, **_: Any) -> QueryResult:
            return QueryResult(sql=sql, columns=["revenue"], rows=[], row_count=0, duration_ms=1.0)

        monkeypatch.setattr(nodes_module, "execute_readonly", empty)
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[_sql("SELECT 1 AS revenue FROM orders WHERE false")],
        )
        _install(monkeypatch, provider)

        run = run_agent("revenue in 1999", settings=settings)

        assert run.status == "success"
        assert run.row_count == 0
        assert "no rows" in run.answer.lower()
        # The insight node must not have called the model for an empty result.
        assert not [c for c in provider.calls if c[0] == "InsightBundle"]


class TestNodeErrorBoundary:
    def test_an_unexpected_node_error_does_not_crash_the_run(
        self, monkeypatch, settings, stub_execution
    ):
        """A raising node would skip the conditional edges entirely, turning a
        recoverable failure into a crashed request."""

        def explode(_: Any) -> dict[str, Any]:
            raise RuntimeError("boom")

        monkeypatch.setattr(nodes_module, "visualize_node", explode)
        monkeypatch.setattr(
            graph_module, "visualize_node", nodes_module._timed("visualization", explode)
        )

        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[_sql("SELECT 1 AS revenue FROM orders")],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run = run_agent("revenue", settings=settings)

        assert run.status == "error"
        assert "unexpected" in run.error_message.lower()
        assert any(trace["status"] == "error" for trace in run.traces)


class TestConversationState:
    def test_previous_tables_are_carried_into_a_follow_up(
        self, monkeypatch, settings, stub_execution
    ):
        """ "Only the last 6 months" names no table, but the prior turn's remain
        relevant."""
        import datetime as dt

        history = [
            ConversationTurn(
                question="revenue by region",
                resolved_question="revenue by region",
                sql="SELECT 1",
                tables=("orders", "regions"),
                row_count=12,
                headline="EMEA led.",
                asked_at=dt.datetime.now(dt.UTC),
            )
        ]
        provider = ScriptedProvider(
            PlannerDecision=[_plan(resolved_question="revenue by region, last 6 months")],
            SQLGeneration=[_sql("SELECT 1 AS revenue FROM orders")],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run = run_agent("only the last 6 months", history=history, settings=settings)

        assert run.status == "success"
        schema_trace = next(t for t in run.traces if t["node"] == "schema_retrieval")
        assert "regions" in schema_trace["detail"]

    def test_history_is_summarised_not_replayed(self, monkeypatch, settings, stub_execution):
        """An unbounded transcript would grow the prompt without limit."""
        import datetime as dt

        history = [
            ConversationTurn(
                question=f"question {index}",
                resolved_question=f"question {index}",
                sql="SELECT " + "x" * 500,
                tables=("orders",),
                row_count=1,
                headline=f"answer {index}",
                asked_at=dt.datetime.now(dt.UTC),
            )
            for index in range(20)
        ]
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[_sql("SELECT 1 AS revenue FROM orders")],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        run_agent("follow up", history=history, settings=settings)

        planner_prompt = next(p for name, p in provider.calls if name == "PlannerDecision")
        # Bounded by conversation_max_turns (10 by default), not 20.
        assert planner_prompt.count("Q: question") <= 10
        # And the full SQL text of old turns is never replayed.
        assert "x" * 500 not in planner_prompt


class TestStateInitialisation:
    def test_new_state_has_every_accumulator(self):
        state = new_state("q", request_id="abc")
        assert state["attempts"] == []
        assert state["traces"] == []
        assert state["retry_count"] == 0
        assert state["step_count"] == 0

    def test_run_produces_a_conversation_turn(self, monkeypatch, settings, stub_execution):
        provider = ScriptedProvider(
            PlannerDecision=[_plan()],
            SQLGeneration=[_sql("SELECT 1 AS revenue FROM orders")],
            InsightBundle=[_insight()],
        )
        _install(monkeypatch, provider)

        turn = run_agent("revenue", settings=settings).to_turn()

        assert turn.question == "revenue"
        assert turn.headline
        assert turn.tables
