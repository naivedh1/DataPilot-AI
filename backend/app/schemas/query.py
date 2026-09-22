"""The public API contract.

Kept separate from `app/schemas/agent.py`, which describes what the *model*
returns. These describe what the *client* sees. Keeping them apart means the
wire format can stay stable while prompts and agent internals change freely.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.agents.runner import AgentRun

#: Long enough for a real analytical question, short enough that a pasted
#: document cannot be used to drive up token spend.
MAX_QUESTION_LENGTH = 1000


class QueryRequest(BaseModel):
    """A question to answer."""

    question: Annotated[str, Field(min_length=3, max_length=MAX_QUESTION_LENGTH)] = Field(
        description="A business question in plain English.",
        examples=["Show me monthly revenue by region for the last 12 months"],
    )
    conversation_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Continues an existing conversation, enabling follow-up questions. "
            "Omit to start a new one."
        ),
    )

    @field_validator("question")
    @classmethod
    def _strip_and_check(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("question cannot be blank")
        return cleaned


class ExecutionMeta(BaseModel):
    """What actually happened, so the answer can be audited."""

    status: str = Field(description="success, no_query, or error.")
    row_count: int
    truncated: bool = Field(description="True when the row cap trimmed the result set.")
    execution_ms: float = Field(description="Time spent in the database.")
    total_ms: float = Field(description="End-to-end time for the whole run.")
    retries: int = Field(description="SQL repair attempts made.")
    llm_calls: int
    llm_simulated: bool = Field(
        description=(
            "True when answered by the deterministic offline baseline rather than a language model."
        )
    )
    tables_used: list[str] = Field(default_factory=list)
    intent: str = ""


class NodeTraceModel(BaseModel):
    """One agent node's execution record."""

    node: str
    duration_ms: float
    status: str
    detail: str = ""


class SQLAttemptModel(BaseModel):
    """One attempt at producing executable SQL."""

    sql: str
    valid: bool
    executed: bool
    error: str = ""
    stage: str


class InsightModel(BaseModel):
    """One grounded observation."""

    text: str
    kind: Literal["finding", "trend", "concern", "recommendation"]
    supporting_values: list[str] = Field(default_factory=list)


class ChartSeriesModel(BaseModel):
    name: str
    values: list[Any]


class ChartModel(BaseModel):
    """A chart specification the frontend renders with Plotly."""

    chart_type: str
    title: str
    x_label: str = ""
    y_label: str = ""
    x_values: list[Any] = Field(default_factory=list)
    series: list[ChartSeriesModel] = Field(default_factory=list)
    rationale: str = ""
    value_format: str = "number"


class ValidationCheckModel(BaseModel):
    """One deterministic check run against the result."""

    name: str
    status: str = Field(description="passed, failed, or skipped.")
    detail: str = ""


class ValidationModel(BaseModel):
    """What the result validator found."""

    status: str = Field(description="passed, failed, or not_verified.")
    checks: list[ValidationCheckModel] = Field(default_factory=list)


class ConfidenceSignalModel(BaseModel):
    """One input to the confidence assessment, and the ceiling it imposed."""

    name: str
    ceiling: str
    detail: str


class ConfidenceModel(BaseModel):
    """Why the answer carries the confidence it does.

    Computed by `app/services/confidence.py` from what the run actually did.
    No part of it is supplied by the language model.
    """

    level: str = Field(description="high, medium, low, or insufficient_data.")
    rationale: str = Field(description="Plain-language reason for the level.")
    signals: list[ConfidenceSignalModel] = Field(default_factory=list)


class InvestigationStepModel(BaseModel):
    """One query the diagnostic ran, and what it was for."""

    name: str
    purpose: str
    sql: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    error: str = ""


class ContributorModel(BaseModel):
    """One dimension value's share of the overall change."""

    dimension: str
    label: str
    change: float
    share_of_change: float


class InvestigationModel(BaseModel):
    """A multi-step diagnostic, with every query it ran.

    Present only for "why did X change" questions. Every figure was computed
    by `app/services/investigation.py` from templated SQL; the model chose
    what to investigate but wrote none of the queries and none of the numbers.
    """

    plan: dict[str, Any] = Field(default_factory=dict)
    steps: list[InvestigationStepModel] = Field(default_factory=list)
    current_value: float = 0.0
    previous_value: float = 0.0
    change: float = 0.0
    percent_change: float | None = None
    contributors: list[ContributorModel] = Field(default_factory=list)
    reconciled: bool | None = Field(
        default=None, description="Whether the breakdowns sum to the headline change."
    )
    caveats: list[str] = Field(default_factory=list)


class QueryResponse(BaseModel):
    """The full answer, with its evidence."""

    request_id: str
    conversation_id: str
    question: str
    answer: str = Field(description="A direct one-sentence answer.")
    insights: list[InsightModel] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list, description="Limitations of this answer.")
    sql: str = Field(default="", description="The SQL that produced the result.")
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    chart: ChartModel | None = None
    reasoning_summary: str = ""
    validation: ValidationModel | None = Field(
        default=None, description="Deterministic checks run against the returned figures."
    )
    confidence: ConfidenceModel | None = Field(
        default=None, description="How much weight the answer can bear, and why."
    )
    investigation: InvestigationModel | None = Field(
        default=None,
        description="The diagnostic trail, for a 'why did X change' question.",
    )
    execution: ExecutionMeta
    attempts: list[SQLAttemptModel] = Field(
        default_factory=list, description="Every SQL attempt, including rejected ones."
    )
    trace: list[NodeTraceModel] = Field(
        default_factory=list, description="Which agent nodes ran, and for how long."
    )
    error: str | None = Field(
        default=None, description="A safe message when the run did not succeed."
    )

    @classmethod
    def from_run(cls, run: AgentRun, conversation_id: str) -> QueryResponse:
        """Build the wire response from a finished agent run."""
        return cls(
            request_id=run.request_id,
            conversation_id=conversation_id,
            question=run.question,
            answer=run.answer,
            insights=[InsightModel(**item) for item in run.insights],
            caveats=run.caveats,
            sql=run.sql,
            columns=run.columns,
            rows=run.rows,
            chart=ChartModel(**run.chart) if run.chart else None,
            reasoning_summary=run.reasoning_summary,
            validation=ValidationModel(**run.validation) if run.validation else None,
            confidence=ConfidenceModel(**run.confidence) if run.confidence else None,
            investigation=(InvestigationModel(**run.investigation) if run.investigation else None),
            execution=ExecutionMeta(
                status=run.status,
                row_count=run.row_count,
                truncated=run.truncated,
                execution_ms=run.execution_ms,
                total_ms=run.total_ms,
                retries=run.retries,
                llm_calls=run.llm_calls,
                llm_simulated=run.llm_simulated,
                tables_used=run.tables_used,
                intent=run.intent,
            ),
            attempts=[SQLAttemptModel(**attempt) for attempt in run.attempts],
            trace=[NodeTraceModel(**trace) for trace in run.trace_dicts()],
            error=run.error_message or None,
        )


class ConversationSummary(BaseModel):
    """A conversation as shown in the sidebar."""

    conversation_id: str
    title: str
    turn_count: int
    created_at: str
    updated_at: str


class HistoryItem(BaseModel):
    """A past query, for the history panel."""

    request_id: str
    question: str
    answer: str
    status: str
    row_count: int
    total_ms: float
    sql: str = ""


class HistoryResponse(BaseModel):
    conversations: list[ConversationSummary] = Field(default_factory=list)
    queries: list[HistoryItem] = Field(default_factory=list)


class ColumnSchema(BaseModel):
    name: str
    type: str
    nullable: bool
    primary_key: bool
    foreign_key: str | None = None
    description: str
    allowed_values: list[str] = Field(default_factory=list)


class TableSchema(BaseModel):
    name: str
    description: str
    columns: list[ColumnSchema]
    notes: list[str] = Field(default_factory=list)


class MetricSchema(BaseModel):
    name: str
    description: str
    expression: str
    required_tables: list[str]
    caveat: str | None = None


class SchemaResponse(BaseModel):
    """The warehouse as the assistant understands it."""

    tables: list[TableSchema]
    metrics: list[MetricSchema]
    join_paths: list[str] = Field(default_factory=list)


class SuggestionsResponse(BaseModel):
    """Starter questions for the empty state."""

    suggestions: list[str]
