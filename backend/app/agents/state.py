"""The state every agent node reads from and writes to.

A single typed object rather than ad-hoc kwargs between nodes. That makes the
whole run inspectable — the API returns the evidence trail built up here, and a
failed run can be reconstructed from its final state alone.

`TypedDict` with `total=False` is LangGraph's convention: nodes return a partial
dict and the framework merges it, so a node declares only what it changed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, TypedDict

from app.schemas.agent import ChartSpec, InsightBundle, PlannerDecision, SQLGeneration


@dataclass(frozen=True, slots=True)
class NodeTrace:
    """One node's execution record.

    The trace is the "useful evidence behind the answer" the product promises,
    and it is also the whole of the observability story for an agent run: which
    nodes ran, in what order, how long each took, and what it decided.
    """

    node: str
    duration_ms: float
    status: str = "ok"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SQLAttempt:
    """One attempt at producing executable SQL.

    Kept for every attempt, not just the successful one, so the retry loop is
    visible in the response and measurable in evaluation.
    """

    sql: str
    valid: bool
    executed: bool
    error: str = ""
    stage: str = "generation"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "valid": self.valid,
            "executed": self.executed,
            "error": self.error,
            "stage": self.stage,
        }


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """A compact record of a previous turn.

    Deliberately *not* the full transcript. A follow-up is resolved against this
    structured summary, which keeps the prompt bounded no matter how long the
    conversation runs, and makes follow-up resolution independently testable.
    """

    question: str
    resolved_question: str
    sql: str
    tables: tuple[str, ...]
    row_count: int
    headline: str
    asked_at: dt.datetime

    def render(self) -> str:
        return (
            f"Q: {self.question}\n"
            f"   (resolved as: {self.resolved_question})\n"
            f"   tables: {', '.join(self.tables) or 'none'}; "
            f"rows returned: {self.row_count}\n"
            f"   answer: {self.headline}"
        )


class AgentState(TypedDict, total=False):
    """Everything an agent run accumulates."""

    # -- input -------------------------------------------------------------
    question: str
    request_id: str
    conversation_id: str
    history: list[ConversationTurn]

    # -- planning ----------------------------------------------------------
    plan: PlannerDecision | None
    schema_tables: list[str]
    schema_rendered: str

    # -- SQL ---------------------------------------------------------------
    generation: SQLGeneration | None
    sql: str
    validated_sql: str
    attempts: list[SQLAttempt]
    retry_count: int
    last_error: str

    # -- execution ---------------------------------------------------------
    columns: list[str]
    rows: list[tuple[Any, ...]]
    row_count: int
    truncated: bool
    execution_ms: float

    # -- verification ------------------------------------------------------
    validation: dict[str, Any] | None
    confidence: dict[str, Any] | None

    # -- downstream --------------------------------------------------------
    analysis_summary: str
    analysis: dict[str, Any] | None
    chart: dict[str, Any] | None
    chart_spec: ChartSpec | None
    insights: InsightBundle | None

    # -- outcome -----------------------------------------------------------
    status: str
    error_message: str
    error_code: str
    traces: list[NodeTrace]
    llm_calls: int
    llm_simulated: bool
    step_count: int


def new_state(
    question: str,
    *,
    request_id: str,
    conversation_id: str = "",
    history: list[ConversationTurn] | None = None,
) -> AgentState:
    """An initial state with every accumulator ready to append to."""
    return AgentState(
        question=question,
        request_id=request_id,
        conversation_id=conversation_id,
        history=history or [],
        attempts=[],
        retry_count=0,
        traces=[],
        llm_calls=0,
        llm_simulated=False,
        step_count=0,
        status="running",
        rows=[],
        columns=[],
        row_count=0,
    )


@dataclass(slots=True)
class RunMetrics:
    """Aggregate timings for one run, for logging and the API response."""

    total_ms: float = 0.0
    sql_generation_ms: float = 0.0
    execution_ms: float = 0.0
    llm_ms: float = 0.0
    retries: int = 0
    nodes_run: list[str] = field(default_factory=list)
