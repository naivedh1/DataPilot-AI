"""The agent nodes.

Each node is a function of `AgentState` returning a partial state update. They
own no clients: the provider is fetched through `get_provider()`, which tests
monkeypatch, so every node runs without a network.

Nodes never raise for expected failures. A node that cannot do its job records
the reason in state and lets the graph's routing decide what happens next —
retry, or terminate with a safe message. Raising would bypass the retry edges
entirely, which is the whole reason the graph exists.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from app.agents.state import AgentState, NodeTrace, SQLAttempt
from app.core.config import get_settings
from app.core.exceptions import SQLExecutionError
from app.database.executor import execute_readonly
from app.schemas.agent import (
    AnalysisIntent,
    InsightBundle,
    InsightItem,
    PlannerDecision,
    SQLGeneration,
)
from app.services import analysis as analysis_service
from app.services import confidence as confidence_service
from app.services import validation as validation_service
from app.services import visualization
from app.services.llm import get_provider
from app.services.llm.prompts import (
    INSIGHT_SYSTEM,
    PLANNER_SYSTEM,
    SQL_SYSTEM,
    build_insight_prompt,
    build_planner_prompt,
    build_sql_prompt,
)
from app.services.schema.retrieval import match_metrics, retrieve

logger = logging.getLogger(__name__)


def _timed(
    name: str, function: Callable[[AgentState], dict[str, Any]]
) -> Callable[[AgentState], dict[str, Any]]:
    """Wrap a node so every run is traced and no node can raise into the graph.

    A node raising would skip the conditional edges entirely, turning a
    recoverable failure into a crashed run. Catching here means an unexpected
    error still terminates cleanly, with a safe message and a trace entry.
    """

    def wrapped(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            update = function(state)
            status, detail = "ok", str(update.get("_trace_detail", ""))
        except Exception as error:
            logger.exception("node %s failed unexpectedly", name)
            status, detail = "error", type(error).__name__
            update = {
                "status": "error",
                "error_code": "internal_error",
                "error_message": "An unexpected error occurred while answering.",
            }

        update.pop("_trace_detail", None)
        duration_ms = (time.perf_counter() - started) * 1000
        traces = [*state.get("traces", []), NodeTrace(name, duration_ms, status, detail)]
        return {
            **update,
            "traces": traces,
            "step_count": state.get("step_count", 0) + 1,
        }

    wrapped.__name__ = name
    return wrapped


def _render_history(state: AgentState, limit: int) -> str:
    """The last few turns, as compact structured summaries.

    Never the raw transcript: an unbounded history would grow the prompt without
    limit and bury the current question.
    """
    history = state.get("history", [])
    if not history:
        return ""
    return "\n\n".join(turn.render() for turn in history[-limit:])


# ---------------------------------------------------------------------------
# 1. Planner
# ---------------------------------------------------------------------------


def _plan(state: AgentState) -> dict[str, Any]:
    settings = get_settings()
    provider = get_provider()
    question = state["question"]

    # A cheap first pass at retrieval gives the planner a sense of what data
    # exists, so it can tell an unanswerable question from a hard one.
    preview = retrieve(question)
    context = _render_history(state, settings.conversation_max_turns)

    plan, usage = provider.complete_json(
        system=PLANNER_SYSTEM,
        prompt=build_planner_prompt(
            question,
            schema_summary=", ".join(preview.table_names),
            conversation_context=context,
        ),
        schema=PlannerDecision,
    )

    if plan.intent is AnalysisIntent.UNSUPPORTED or not plan.needs_sql:
        reason = plan.unsupported_reason or ("This question does not require querying the data.")
        return {
            "plan": plan,
            "status": "no_query",
            "error_message": reason,
            "llm_calls": state.get("llm_calls", 0) + 1,
            "llm_simulated": usage.simulated,
            "_trace_detail": f"intent={plan.intent.value}, no SQL required",
        }

    return {
        "plan": plan,
        "llm_calls": state.get("llm_calls", 0) + 1,
        "llm_simulated": usage.simulated,
        "_trace_detail": f"intent={plan.intent.value}, grain={plan.time_grain}",
    }


# ---------------------------------------------------------------------------
# 2. Schema retrieval
# ---------------------------------------------------------------------------


def _retrieve_schema(state: AgentState) -> dict[str, Any]:
    plan = state.get("plan")
    question = plan.resolved_question if plan else state["question"]

    # Carry the previous turn's tables forward: a follow-up like "only the last
    # six months" names no table at all, but the earlier ones remain relevant.
    history = state.get("history", [])
    carried = tuple(history[-1].tables) if history else ()

    schema = retrieve(question, extra_tables=carried)
    return {
        "schema_tables": list(schema.table_names),
        "schema_rendered": schema.render(),
        "_trace_detail": f"tables={', '.join(schema.table_names)}",
    }


# ---------------------------------------------------------------------------
# 3. SQL generation
# ---------------------------------------------------------------------------


def _generate_sql(state: AgentState) -> dict[str, Any]:
    provider = get_provider()
    plan = state.get("plan")
    question = plan.resolved_question if plan else state["question"]

    schema = retrieve(question, extra_tables=tuple(state.get("schema_tables", ())))

    # On a retry, the previous SQL and the error that killed it both go into the
    # prompt. Withholding the error would make the retry a blind re-roll.
    previous = state.get("sql") or None
    error = state.get("last_error") or None

    generation, usage = provider.complete_json(
        system=SQL_SYSTEM,
        prompt=build_sql_prompt(
            question,
            schema=schema,
            plan=plan,
            previous_attempt=previous,
            error=error,
        ),
        schema=SQLGeneration,
    )

    if not generation.sql.strip():
        return {
            "generation": generation,
            "status": "no_query",
            "error_message": (
                generation.reasoning_summary
                or "This question cannot be answered from the available data."
            ),
            "llm_calls": state.get("llm_calls", 0) + 1,
            "llm_simulated": usage.simulated,
            "_trace_detail": "model declined to generate SQL",
        }

    return {
        "generation": generation,
        "sql": generation.sql,
        "llm_calls": state.get("llm_calls", 0) + 1,
        "llm_simulated": usage.simulated,
        "_trace_detail": f"{len(generation.tables_used)} tables referenced",
    }


# ---------------------------------------------------------------------------
# 4. Validation
# ---------------------------------------------------------------------------


def _validate_sql(state: AgentState) -> dict[str, Any]:
    from app.services.sql.validator import validate

    settings = get_settings()
    sql = state.get("sql", "")

    # The allow-list is the retrieved schema, so a query touching a table that
    # was never retrieved is caught here rather than by the database.
    result = validate(
        sql,
        allowed_tables=frozenset(state.get("schema_tables", ())),
        max_rows=settings.sql_max_result_rows,
    )

    attempt = SQLAttempt(
        sql=sql,
        valid=result.is_valid,
        executed=False,
        error=result.reason,
        stage="validation",
    )
    attempts = [*state.get("attempts", []), attempt]

    if not result.is_valid:
        logger.info("SQL rejected by validator: %s", result.reason)
        return {
            "attempts": attempts,
            "last_error": result.reason,
            "_trace_detail": f"rejected: {result.reason[:80]}",
        }

    return {
        "attempts": attempts,
        "validated_sql": result.normalized_sql,
        "last_error": "",
        "_trace_detail": (
            f"accepted; tables={', '.join(sorted(result.tables))}"
            + (" (row cap applied)" if result.limit_applied else "")
        ),
    }


# ---------------------------------------------------------------------------
# 5. Execution
# ---------------------------------------------------------------------------


def _execute_sql(state: AgentState) -> dict[str, Any]:
    sql = state.get("validated_sql") or state.get("sql", "")
    try:
        result = execute_readonly(sql)
    except SQLExecutionError as error:
        attempt = SQLAttempt(
            sql=sql,
            valid=True,
            executed=False,
            error=error.safe_message,
            stage="execution",
        )
        return {
            "attempts": [*state.get("attempts", []), attempt],
            # The database's own message goes back to the generator: it is the
            # most precise possible description of what to fix.
            "last_error": error.detail[:500],
            "error_message": error.safe_message,
            "error_code": error.code,
            "_trace_detail": f"failed: {error.safe_message[:80]}",
        }

    attempt = SQLAttempt(sql=sql, valid=True, executed=True, stage="execution")
    return {
        "attempts": [*state.get("attempts", []), attempt],
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "execution_ms": result.duration_ms,
        "last_error": "",
        "error_message": "",
        "_trace_detail": f"{result.row_count} rows in {result.duration_ms:.0f}ms",
    }


# ---------------------------------------------------------------------------
# 6. Result validation
# ---------------------------------------------------------------------------


def _validate_result(state: AgentState) -> dict[str, Any]:
    """Check the returned figures before anything narrates them.

    The reconciliation check costs a second query, so it runs only when the
    answer is a grouped aggregate — the only shape where parts and whole are
    distinct claims that can disagree. When the total query cannot be derived
    safely the check is skipped, never assumed to pass.

    A failure here does not abort the run. The number is still shown; what
    changes is that the answer is no longer allowed to present itself as
    reliable, which `confidence.py` enforces.
    """
    columns = state.get("columns", [])
    rows = state.get("rows", [])

    # The *pre-normalised* SQL, deliberately. The validator appends a row cap
    # to every query, and a LIMIT is how `build_total_query` recognises a
    # genuine top-N — where the parts are not meant to sum to the whole.
    # Reading `validated_sql` here would make every query look like a top-N
    # and skip reconciliation on all of them.
    sql = state.get("sql", "")

    total_columns: list[str] | None = None
    total_rows: list[tuple[Any, ...]] | None = None

    total_sql = validation_service.build_total_query(sql) if sql else None
    if total_sql:
        try:
            total = execute_readonly(total_sql)
            total_columns, total_rows = total.columns, total.rows
        except SQLExecutionError as error:
            # A cross-check that cannot run leaves the claim unverified. That
            # is a weaker answer, not a failed one.
            logger.info("reconciliation query failed: %s", error.safe_message[:120])

    report = validation_service.validate_result(
        columns=columns,
        rows=rows,
        row_count=state.get("row_count", 0),
        truncated=state.get("truncated", False),
        total_columns=total_columns,
        total_rows=total_rows,
    )

    failed = len(report.failed)
    return {
        "validation": report.to_dict(),
        "_trace_detail": (
            f"{len(report.passed)} passed, {failed} failed, {len(report.skipped)} skipped"
        ),
    }


# ---------------------------------------------------------------------------
# 7. Analysis
# ---------------------------------------------------------------------------


def _analyse(state: AgentState) -> dict[str, Any]:
    from app.database.executor import QueryResult

    result = QueryResult(
        sql=state.get("validated_sql", ""),
        columns=state.get("columns", []),
        rows=state.get("rows", []),
        row_count=state.get("row_count", 0),
        duration_ms=state.get("execution_ms", 0.0),
    )
    report = analysis_service.analyse(result)
    # `asdict`, not `__dict__`: the analysis dataclasses use slots=True for
    # compactness, and a slotted instance has no instance dictionary.
    return {
        "analysis_summary": report.summarize(),
        "analysis": {
            "row_count": report.row_count,
            "numeric_columns": report.numeric_columns,
            "trend": asdict(report.trend) if report.trend else None,
            "ranking": asdict(report.ranking) if report.ranking else None,
            "outliers": [asdict(point) for point in report.outliers],
            "correlation": list(report.correlation) if report.correlation else None,
        },
        "_trace_detail": f"{len(report.stats)} numeric columns analysed",
    }


# ---------------------------------------------------------------------------
# 8. Visualization
# ---------------------------------------------------------------------------


def _visualize(state: AgentState) -> dict[str, Any]:
    from app.database.executor import QueryResult

    plan = state.get("plan")
    result = QueryResult(
        sql=state.get("validated_sql", ""),
        columns=state.get("columns", []),
        rows=state.get("rows", []),
        row_count=state.get("row_count", 0),
        duration_ms=state.get("execution_ms", 0.0),
    )
    question = plan.resolved_question if plan else state["question"]
    payload = visualization.select_chart(result, question)
    return {
        "chart": payload.to_dict(),
        "_trace_detail": f"{payload.chart_type}: {payload.rationale[:60]}",
    }


# ---------------------------------------------------------------------------
# 9. Insight
# ---------------------------------------------------------------------------


def _insights(state: AgentState) -> dict[str, Any]:
    provider = get_provider()
    plan = state.get("plan")
    question = plan.resolved_question if plan else state["question"]
    rows = state.get("rows", [])

    if not rows:
        # Nothing to ground an insight in. Saying so is the correct answer;
        # asking the model to comment on an empty result invites invention.
        return {
            "insights": InsightBundle(
                headline="The query returned no rows.",
                insights=[
                    InsightItem(
                        text=(
                            "No records match these criteria. The filters may be "
                            "too narrow, or the period may fall outside the data."
                        ),
                        kind="finding",
                    )
                ],
                caveats=["No data was returned, so no analysis is possible."],
            ),
            "_trace_detail": "empty result; no insights generated",
        }

    bundle, usage = provider.complete_json(
        system=INSIGHT_SYSTEM,
        prompt=build_insight_prompt(
            question,
            sql=state.get("validated_sql", ""),
            columns=state.get("columns", []),
            rows=rows,
            row_count=state.get("row_count", 0),
            analysis_summary=state.get("analysis_summary", ""),
        ),
        schema=InsightBundle,
    )
    return {
        "insights": bundle,
        "llm_calls": state.get("llm_calls", 0) + 1,
        "llm_simulated": usage.simulated,
        "_trace_detail": f"{len(bundle.insights)} insights",
    }


# ---------------------------------------------------------------------------
# 10. Response
# ---------------------------------------------------------------------------


def _assess_confidence(state: AgentState, status: str) -> dict[str, Any]:
    """Score the run from what it actually did.

    Computed here rather than anywhere upstream because the inputs are only
    all present at the end: the retry count is final, the result has been
    validated, and the terminal status is known.
    """
    plan = state.get("plan")
    question = plan.resolved_question if plan else state.get("question", "")

    report = None
    raw_validation = state.get("validation")
    if raw_validation is not None:
        report = validation_service.ValidationReport(
            tuple(
                validation_service.ValidationCheck(
                    name=check["name"],
                    status=validation_service.CheckStatus(check["status"]),
                    detail=check.get("detail", ""),
                )
                for check in raw_validation.get("checks", [])
            )
        )

    assessment = confidence_service.assess(
        status=status,
        row_count=state.get("row_count", 0),
        metric_matched=bool(match_metrics(question)),
        ambiguous=bool(plan and plan.unsupported_reason) or status == "no_query",
        retry_count=state.get("retry_count", 0),
        validation=report,
    )
    return assessment.to_dict()


def _respond(state: AgentState) -> dict[str, Any]:
    """Finalise the run. Purely local — no model call."""
    status = state.get("status", "success")
    if status in ("error", "no_query"):
        return {
            "confidence": _assess_confidence(state, status),
            "_trace_detail": f"terminal: {status}",
        }
    confidence = _assess_confidence(state, "success")
    return {
        "status": "success",
        "confidence": confidence,
        "_trace_detail": f"answer assembled, confidence={confidence['level']}",
    }


def _fail(state: AgentState) -> dict[str, Any]:
    """Terminate after the retry budget is spent."""
    attempts = state.get("attempts", [])
    message = (
        state.get("error_message")
        or state.get("last_error")
        or ("The query could not be completed.")
    )
    logger.info("run failed after %s attempts: %s", len(attempts), message[:200])
    return {
        "status": "error",
        "error_code": state.get("error_code") or "sql_generation_failed",
        "error_message": _safe(message),
        "confidence": _assess_confidence(state, "error"),
        "_trace_detail": f"gave up after {len(attempts)} attempts",
    }


def _safe(message: str) -> str:
    """Keep raw driver text out of a user-facing message.

    Validator reasons are written to be shown; database errors are not, so the
    detail stays in the log and the user gets a summary.
    """
    lowered = message.lower()
    if any(token in lowered for token in ("postgresql://", "password", 'role "')):
        return "The query could not be completed."
    return message[:300]


# Exported node callables, wrapped for tracing.
plan_node = _timed("planner", _plan)
schema_node = _timed("schema_retrieval", _retrieve_schema)
generate_node = _timed("sql_generation", _generate_sql)
validate_node = _timed("sql_validation", _validate_sql)
execute_node = _timed("sql_execution", _execute_sql)
result_validation_node = _timed("result_validation", _validate_result)
analyse_node = _timed("analysis", _analyse)
visualize_node = _timed("visualization", _visualize)
insight_node = _timed("insight", _insights)
respond_node = _timed("response", _respond)
fail_node = _timed("failure", _fail)
