"""Structured outputs the agent nodes exchange with the language model.

Every model the code branches on is defined here as a Pydantic schema and
validated at the boundary. A node receiving one of these may assume it is
well-formed; a malformed response fails where it is produced, not three nodes
later where the symptom would be unrecognisable.

`reasoning_summary` fields are deliberately short and user-facing. They are a
*summary* of what was decided and why — not hidden chain-of-thought, which is
neither exposed in the UI nor stored.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class AnalysisIntent(StrEnum):
    """What kind of analytical work a question calls for.

    Drives routing: an `AGGREGATION` needs a chart and a headline number, a
    `TREND` needs period-over-period comparison, a `SCHEMA_QUESTION` needs no
    SQL at all.
    """

    AGGREGATION = "aggregation"
    TREND = "trend"
    RANKING = "ranking"
    COMPARISON = "comparison"
    DISTRIBUTION = "distribution"
    ANOMALY = "anomaly"
    LOOKUP = "lookup"
    SCHEMA_QUESTION = "schema_question"
    UNSUPPORTED = "unsupported"


class PlannerDecision(BaseModel):
    """The planner's reading of the question."""

    intent: AnalysisIntent = Field(description="The analytical task this question calls for.")
    needs_sql: bool = Field(description="Whether answering requires querying the warehouse.")
    needs_analysis: bool = Field(
        description="Whether the result needs post-processing beyond what SQL returns."
    )
    needs_chart: bool = Field(
        description="Whether a visualization would genuinely help, rather than decorate."
    )
    entities: list[str] = Field(
        default_factory=list,
        description="Business nouns mentioned: metrics, dimensions, filters.",
    )
    time_grain: Literal["day", "week", "month", "quarter", "year", "none"] = Field(
        default="none", description="Time granularity the answer should use."
    )
    resolved_question: str = Field(
        description=(
            "The question rewritten to stand alone. For a follow-up, prior "
            "context is folded in so the SQL generator needs no history."
        )
    )
    reasoning_summary: str = Field(
        default="", max_length=400, description="One or two sentences on the approach."
    )
    unsupported_reason: str | None = Field(
        default=None,
        description="If the warehouse cannot answer this, why. Null otherwise.",
    )


class SQLGeneration(BaseModel):
    """A candidate query."""

    sql: str = Field(description="A single read-only PostgreSQL SELECT statement.")
    tables_used: list[str] = Field(default_factory=list, description="Tables the query reads.")
    metrics: list[str] = Field(
        default_factory=list, description="Business metrics the query computes."
    )
    reasoning_summary: str = Field(
        default="",
        max_length=400,
        description="Brief note on how the query was constructed.",
    )


class InsightItem(BaseModel):
    """One grounded observation about the returned data."""

    text: str = Field(max_length=400, description="The finding, in plain business language.")
    kind: Literal["finding", "trend", "concern", "recommendation"] = Field(
        default="finding",
        description=(
            "Separates what the data shows from what someone might do about it. "
            "A recommendation is an opinion and is labelled as one."
        ),
    )
    supporting_values: list[str] = Field(
        default_factory=list,
        description="Figures cited, exactly as they appear in the result set.",
    )


class InsightBundle(BaseModel):
    """The insight node's output."""

    headline: str = Field(
        max_length=300, description="A direct one-sentence answer to the question."
    )
    insights: list[InsightItem] = Field(
        default_factory=list, description="Supporting observations."
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="Limitations of this answer: filters applied, data not covered.",
    )


class ChartSpec(BaseModel):
    """A chart the frontend can render directly."""

    chart_type: Literal["line", "bar", "grouped_bar", "scatter", "histogram", "pie", "table"]
    title: str
    x_column: str | None = None
    y_columns: list[str] = Field(default_factory=list)
    series_column: str | None = Field(
        default=None, description="Column to split into multiple series, if any."
    )
    x_label: str | None = None
    y_label: str | None = None
    rationale: str = Field(
        default="", max_length=300, description="Why this chart form suits this result."
    )
