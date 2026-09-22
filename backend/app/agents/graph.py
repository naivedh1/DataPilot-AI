"""The LangGraph workflow.

The graph is where error recovery actually lives. Two edges route failure back
to SQL generation — one from validation, one from execution — each carrying the
reason for the failure so the retry is informed rather than a re-roll.

**No loop is unbounded.** Both retry edges consult `retry_count` against
`SQL_MAX_RETRIES` and fall through to a terminal failure node once the budget is
spent. A third guard, `AGENT_MAX_STEPS`, caps total node executions, so even a
routing bug cannot produce an infinite run.

    planner ─▶ schema ─▶ generate ─▶ validate ─┬(ok)▶ execute ─┬(ok)▶ check ─▶ analyse
       │                       ▲                   │                   │
       └─(no SQL needed)──▶ respond                │                   └─(error, budget)─┐
                               ▲                   └─(invalid, budget)──────────────────┤
                               │                                                        │
                               └──────── failure ◀──────────────────────────────────────┘
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.nodes import (
    analyse_node,
    execute_node,
    fail_node,
    generate_node,
    insight_node,
    plan_node,
    respond_node,
    result_validation_node,
    schema_node,
    validate_node,
    visualize_node,
)
from app.agents.state import AgentState
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _after_planning(state: AgentState) -> Literal["schema_retrieval", "response"]:
    """Skip the SQL path for questions that need no query."""
    if state.get("status") in ("no_query", "error"):
        return "response"
    return "schema_retrieval"


def _after_generation(state: AgentState) -> Literal["sql_validation", "response"]:
    """A model that declined to write SQL has given its answer."""
    if state.get("status") in ("no_query", "error"):
        return "response"
    return "sql_validation"


def _make_retry_router(settings: Settings, success: str) -> Callable[[AgentState], str]:
    """Build a router that retries on failure while budget remains.

    Shared by validation and execution: both need identical bounded-retry
    semantics, and duplicating the budget arithmetic is how one of them
    eventually drifts into an unbounded loop.
    """

    def route(state: AgentState) -> str:
        failed = bool(state.get("last_error"))
        if not failed:
            return success

        retries = state.get("retry_count", 0)
        steps = state.get("step_count", 0)

        if retries >= settings.sql_max_retries:
            logger.info("retry budget exhausted (%s/%s)", retries, settings.sql_max_retries)
            return "failure"
        if steps >= settings.agent_max_steps:
            # A backstop against a routing bug, independent of the retry count.
            logger.warning("agent step ceiling reached (%s)", steps)
            return "failure"

        return "sql_generation"

    return route


def _increment_retry(state: AgentState) -> dict[str, int]:
    """Count a retry. A separate node so the counter cannot be forgotten."""
    return {"retry_count": state.get("retry_count", 0) + 1}


def build_graph(settings: Settings | None = None) -> CompiledStateGraph:
    """Compile the agent workflow.

    Accepting settings makes the retry budget injectable, so tests can pin it to
    zero (to measure first-attempt quality) or to one (to exercise the repair
    path) without touching process-wide configuration.
    """
    settings = settings or get_settings()
    graph: StateGraph = StateGraph(AgentState)

    graph.add_node("planner", plan_node)
    graph.add_node("schema_retrieval", schema_node)
    graph.add_node("sql_generation", generate_node)
    graph.add_node("sql_validation", validate_node)
    graph.add_node("sql_execution", execute_node)
    graph.add_node("result_validation", result_validation_node)
    graph.add_node("analysis", analyse_node)
    graph.add_node("visualization", visualize_node)
    graph.add_node("insight", insight_node)
    graph.add_node("response", respond_node)
    graph.add_node("failure", fail_node)
    graph.add_node("count_retry", _increment_retry)

    graph.set_entry_point("planner")

    graph.add_conditional_edges(
        "planner",
        _after_planning,
        {"schema_retrieval": "schema_retrieval", "response": "response"},
    )
    graph.add_edge("schema_retrieval", "sql_generation")
    graph.add_conditional_edges(
        "sql_generation",
        _after_generation,
        {"sql_validation": "sql_validation", "response": "response"},
    )

    # Validation failure -> count the retry, then regenerate with the reason.
    validation_router = _make_retry_router(settings, success="sql_execution")
    execution_router = _make_retry_router(settings, success="result_validation")

    graph.add_conditional_edges(
        "sql_validation",
        validation_router,
        {
            "sql_execution": "sql_execution",
            "sql_generation": "count_retry",
            "failure": "failure",
        },
    )

    # Execution failure -> the same bounded path, carrying the database error.
    graph.add_conditional_edges(
        "sql_execution",
        execution_router,
        {
            "result_validation": "result_validation",
            "sql_generation": "count_retry",
            "failure": "failure",
        },
    )

    graph.add_edge("count_retry", "sql_generation")
    graph.add_edge("result_validation", "analysis")
    graph.add_edge("analysis", "visualization")
    graph.add_edge("visualization", "insight")
    graph.add_edge("insight", "response")
    graph.add_edge("response", END)
    graph.add_edge("failure", END)

    return graph.compile()


#: Node execution ceiling passed to LangGraph. Set above `agent_max_steps` so
#: the application's own guard fires first and produces a clean failure, rather
#: than LangGraph raising a recursion error.
def recursion_limit(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    return settings.agent_max_steps + 10
