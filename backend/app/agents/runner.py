"""Running the agent and shaping its result.

Separated from the graph so the graph stays purely about wiring, and so the API
has one entry point that always returns a well-formed answer — including when
the run failed.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from app.agents.graph import build_graph, recursion_limit
from app.agents.state import AgentState, ConversationTurn, new_state
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentRun:
    """The finished result of one question."""

    request_id: str
    question: str
    status: str
    answer: str = ""
    sql: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    chart: dict[str, Any] | None = None
    insights: list[dict[str, Any]] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    tables_used: list[str] = field(default_factory=list)
    intent: str = ""
    reasoning_summary: str = ""
    error_message: str = ""
    error_code: str = ""
    retries: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)
    traces: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, Any] | None = None
    confidence: dict[str, Any] | None = None
    total_ms: float = 0.0
    execution_ms: float = 0.0
    llm_calls: int = 0
    llm_simulated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == "success"

    def trace_dicts(self) -> list[dict[str, Any]]:
        """Node traces, already serialised. Named distinctly from `traces` so
        the API layer cannot accidentally serialise dataclasses."""
        return self.traces

    def to_turn(self) -> ConversationTurn:
        """A compact record for the conversation history.

        Only what a follow-up needs to resolve against — not the rows, not the
        chart, not the trace. Keeping this small is what bounds the prompt as a
        conversation grows.
        """
        import datetime as dt

        return ConversationTurn(
            question=self.question,
            resolved_question=self.reasoning_summary or self.question,
            sql=self.sql,
            tables=tuple(self.tables_used),
            row_count=self.row_count,
            headline=self.answer[:300],
            asked_at=dt.datetime.now(dt.UTC),
        )


def run_agent(
    question: str,
    *,
    request_id: str | None = None,
    conversation_id: str = "",
    history: list[ConversationTurn] | None = None,
    settings: Settings | None = None,
) -> AgentRun:
    """Answer one question end to end.

    Never raises for an expected failure: a failed run comes back as an
    `AgentRun` with `status="error"` and a safe message, so the API has one
    response shape regardless of outcome.
    """
    settings = settings or get_settings()
    request_id = request_id or uuid.uuid4().hex[:12]
    started = time.perf_counter()

    logger.info(
        "agent run starting",
        extra={"request_id": request_id, "conversation_id": conversation_id},
    )

    state = new_state(
        question,
        request_id=request_id,
        conversation_id=conversation_id,
        history=history,
    )

    try:
        # LangGraph returns the merged state as a plain dict; AgentState is a
        # TypedDict describing exactly that shape.
        final = cast(
            AgentState,
            build_graph(settings).invoke(
                state, config={"recursion_limit": recursion_limit(settings)}
            ),
        )
    except Exception:
        logger.exception("agent graph failed", extra={"request_id": request_id})
        return AgentRun(
            request_id=request_id,
            question=question,
            status="error",
            error_code="internal_error",
            error_message="An unexpected error occurred while answering.",
            total_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    total_ms = (time.perf_counter() - started) * 1000
    return _shape(final, question, request_id, total_ms)


def _shape(state: AgentState, question: str, request_id: str, total_ms: float) -> AgentRun:
    """Turn a final graph state into the API's answer shape."""
    plan = state.get("plan")
    insights = state.get("insights")
    generation = state.get("generation")
    status = state.get("status", "error")

    answer = ""
    insight_items: list[dict[str, Any]] = []
    caveats: list[str] = []
    if insights is not None:
        answer = insights.headline
        insight_items = [item.model_dump() for item in insights.insights]
        caveats = list(insights.caveats)
    elif status == "no_query":
        answer = state.get("error_message", "")

    if state.get("truncated"):
        caveats.append(
            f"Showing the first {state.get('row_count', 0)} rows; the full result set is larger."
        )

    return AgentRun(
        request_id=request_id,
        question=question,
        status=status,
        answer=answer,
        sql=state.get("validated_sql") or state.get("sql", ""),
        columns=state.get("columns", []),
        rows=[list(row) for row in state.get("rows", [])],
        row_count=state.get("row_count", 0),
        truncated=bool(state.get("truncated")),
        chart=state.get("chart"),
        insights=insight_items,
        caveats=caveats,
        tables_used=(generation.tables_used if generation else state.get("schema_tables", [])),
        intent=plan.intent.value if plan else "",
        reasoning_summary=(
            generation.reasoning_summary if generation else (plan.reasoning_summary if plan else "")
        ),
        error_message=state.get("error_message", ""),
        error_code=state.get("error_code", ""),
        retries=state.get("retry_count", 0),
        attempts=[attempt.to_dict() for attempt in state.get("attempts", [])],
        validation=state.get("validation"),
        confidence=state.get("confidence"),
        traces=[trace.to_dict() for trace in state.get("traces", [])],
        total_ms=round(total_ms, 2),
        execution_ms=state.get("execution_ms", 0.0),
        llm_calls=state.get("llm_calls", 0),
        llm_simulated=bool(state.get("llm_simulated")),
    )
