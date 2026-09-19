"""Query endpoints.

Thin by design. The route validates input, delegates to the agent, records the
turn, and shapes the response. All the logic lives in `app/agents` and
`app/services`, which is what makes those testable without HTTP.

A failed run is **200 with `execution.status == "error"`**, not a 5xx. The
request succeeded; the analysis did not. Returning 500 would make a
model-declined question indistinguishable from a crashed server, and would
discard the evidence trail the client needs to show the user what went wrong.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, status

from app.agents.runner import run_agent
from app.api.deps import RequestIdDep, SettingsDep, StoreDep
from app.schemas.query import (
    ConversationSummary,
    HistoryItem,
    HistoryResponse,
    QueryRequest,
    QueryResponse,
    SuggestionsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["query"])

#: Starter questions for the empty state. Chosen to span the analytical range —
#: trend, ranking, segmentation, comparison, anomaly — so the interface shows
#: what the system can do rather than one cherry-picked demo.
#:
#: These are *inputs*, never answers: each runs through the same agent graph as
#: anything a user types. Nothing here is pre-computed.
SUGGESTED_QUESTIONS: tuple[str, ...] = (
    "Show me monthly revenue by region for the last 12 months",
    "Which 10 products generated the highest revenue this year?",
    "What is the average order value by customer segment?",
    "Which acquisition channel produces the most valuable customers?",
    "What is the return rate by product category?",
    "How did revenue grow month over month in 2025?",
    "Which regions underperformed last quarter?",
    "What is our gross margin by product category?",
)


@router.post(
    "/query",
    response_model=QueryResponse,
    summary="Ask a question of the data",
    description=(
        "Runs the full agent workflow: plan, retrieve schema, generate SQL, "
        "validate, execute read-only, analyse, visualise and explain. Returns "
        "the answer together with the SQL, the rows, the chart specification "
        "and the execution trace."
    ),
)
def create_query(
    payload: QueryRequest,
    settings: SettingsDep,
    store: StoreDep,
    request_id: RequestIdDep,
) -> QueryResponse:
    conversation = store.get_or_create(payload.conversation_id)
    history = store.history_for(conversation.id)

    logger.info(
        "query received",
        extra={
            "request_id": request_id,
            "conversation_id": conversation.id,
            "history_turns": len(history),
            # The question itself is logged by the observability layer, which
            # applies redaction. It is not repeated here.
        },
    )

    run = run_agent(
        payload.question,
        request_id=request_id,
        conversation_id=conversation.id,
        history=history,
        settings=settings,
    )
    store.record(conversation.id, run)

    return QueryResponse.from_run(run, conversation.id)


@router.get(
    "/query/{query_id}",
    response_model=QueryResponse,
    summary="Retrieve a previous query",
)
def get_query(query_id: str, store: StoreDep) -> QueryResponse:
    run = store.get_query(query_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No query was found with that id.",
        )
    return QueryResponse.from_run(run, "")


@router.get(
    "/history",
    response_model=HistoryResponse,
    summary="List conversations and recent queries",
)
def get_history(
    store: StoreDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> HistoryResponse:
    return HistoryResponse(
        conversations=[
            ConversationSummary(**summary) for summary in store.list_conversations(limit=limit)
        ],
        queries=[
            HistoryItem(
                request_id=run.request_id,
                question=run.question,
                answer=run.answer or run.error_message,
                status=run.status,
                row_count=run.row_count,
                total_ms=run.total_ms,
                sql=run.sql,
            )
            for run in store.list_queries(limit=limit)
        ],
    )


@router.post(
    "/conversations",
    summary="Start a new conversation",
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(store: StoreDep) -> dict[str, str]:
    conversation = store.create()
    return {"conversation_id": conversation.id}


@router.get(
    "/suggestions",
    response_model=SuggestionsResponse,
    summary="Starter questions",
)
def get_suggestions() -> SuggestionsResponse:
    return SuggestionsResponse(suggestions=list(SUGGESTED_QUESTIONS))
