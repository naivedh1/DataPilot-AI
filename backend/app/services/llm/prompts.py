"""Prompt construction.

Prompts are built here rather than inline in the nodes so they can be inspected,
diffed and regression-tested like any other code. Two rules hold throughout:

* **Everything the model needs is in the prompt.** It is never asked to recall
  schema, remember a previous turn, or guess a metric definition. Ambiguity the
  prompt fails to resolve becomes a wrong answer.
* **Nothing sensitive is in the prompt.** No credentials, no connection strings,
  no planted-anomaly ground truth.

The `QUESTION:` marker is load-bearing: the offline baseline parses it back out
to drive its rule engine.
"""

from __future__ import annotations

import datetime as dt

from app.schemas.agent import PlannerDecision
from app.services.schema.retrieval import RetrievedSchema

#: The warehouse's fixed window. Relative dates ("last quarter") resolve against
#: this, not against the wall clock — the data is generated for a pinned period,
#: so a model reasoning from today's date would filter to an empty range.
DATA_WINDOW_START = dt.date(2024, 9, 1)
DATA_WINDOW_END = dt.date(2026, 8, 31)


def _window_note() -> str:
    return (
        f"The warehouse holds orders from {DATA_WINDOW_START} to "
        f"{DATA_WINDOW_END} inclusive. Treat {DATA_WINDOW_END} as 'today' when "
        f"resolving relative dates such as 'last month' or 'this year'. Do not "
        f"use CURRENT_DATE or NOW() — the data does not extend to the real "
        f"current date, so those would return nothing."
    )


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = f"""You are the planning component of a data analytics assistant.

Read the user's question and decide what work it requires. You do not write SQL
and you do not answer the question — you classify it.

{_window_note()}

Rules:
- Set needs_sql false only for questions about the schema itself, or for
  questions the warehouse cannot answer at all.
- Set needs_chart true only when a chart would genuinely aid understanding. A
  single number does not need one. A time series or a category comparison does.
- resolved_question must stand alone. If the question is a follow-up, fold the
  prior context into it so that a reader with no history could answer it.
- If the warehouse cannot answer the question, set intent to "unsupported" and
  explain why in unsupported_reason. Do not invent a way to answer it.
- reasoning_summary is one or two plain sentences for the user. It is not
  scratch space.
"""


def build_planner_prompt(
    question: str,
    *,
    schema_summary: str,
    conversation_context: str = "",
) -> str:
    sections = [f"QUESTION: {question}"]
    if conversation_context:
        sections.append(f"CONVERSATION SO FAR:\n{conversation_context}")
    sections.append(f"AVAILABLE DATA:\n{schema_summary}")
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# SQL generation
# ---------------------------------------------------------------------------

SQL_SYSTEM = f"""You write PostgreSQL SELECT queries for a business analytics
warehouse.

{_window_note()}

Hard requirements:
- Emit exactly one statement. No semicolon-separated statements.
- SELECT only. Never INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE,
  GRANT, REVOKE or COPY. The database connection is read-only and any such
  query will be rejected before it runs.
- Use only the tables and columns given in the SCHEMA section. Never invent a
  table, a column, or a value. If the schema cannot answer the question, return
  an empty sql string and say why in reasoning_summary.
- Use the METRIC DEFINITIONS exactly as given. They are the organisation's
  agreed definitions; a different formula produces a different number and a
  wrong answer.
- Use only the literal values listed under "Allowed values" for a column.
  Inventing a status or segment name silently returns zero rows.

Query guidance:
- Alias aggregates with clear, snake_case names — they become chart labels.
- Use date_trunc for period grouping and ORDER BY the period ascending.
- Round monetary aggregates to 2 decimal places.
- Apply LIMIT for "top N" questions.
- Prefer explicit JOIN ... ON over comma joins.

reasoning_summary: one or two sentences on how you built the query. It is shown
to the user and stored in logs. Do not include step-by-step deliberation.
"""


def build_sql_prompt(
    question: str,
    *,
    schema: RetrievedSchema,
    plan: PlannerDecision | None = None,
    previous_attempt: str | None = None,
    error: str | None = None,
) -> str:
    """Compose the SQL-generation prompt.

    When `previous_attempt` and `error` are supplied this becomes a repair
    prompt. The failed SQL and the database's own error message are both
    included: the error text is the most useful possible signal for a fix, and
    withholding it would make the retry a blind re-roll.
    """
    sections = [f"QUESTION: {question}"]

    if plan is not None:
        details = [f"Analytical intent: {plan.intent.value}"]
        if plan.time_grain != "none":
            details.append(f"Time grain: {plan.time_grain}")
        if plan.entities:
            details.append(f"Entities mentioned: {', '.join(plan.entities)}")
        sections.append("PLAN:\n" + "\n".join(f"- {line}" for line in details))

    sections.append(f"SCHEMA:\n{schema.render()}")

    if previous_attempt and error:
        sections.append(
            "PREVIOUS ATTEMPT FAILED.\n"
            f"SQL:\n{previous_attempt}\n\n"
            f"ERROR: {error}\n\n"
            "Fix the specific problem named in the error. Do not restate the "
            "same query, and do not change the question being answered."
        )

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------

INSIGHT_SYSTEM = """You explain query results to a business audience.

You are given a question and the actual rows returned by a SQL query. Explain
what they show.

Absolute rules:
- Every number you state must appear in the result rows, or be a difference or
  percentage you can compute directly from them. Never estimate, never round to
  a "nicer" figure, and never carry a number over from general knowledge.
- When a COMPUTED ANALYSIS section is present, take totals, averages and
  period-over-period changes from it verbatim. Those were calculated from the
  full result set. Do not re-add a column yourself: summing a long list by hand
  is where a confident, slightly wrong figure comes from, and slightly wrong is
  indistinguishable from invented to the person reading it.
- Never assert a cause. The data shows what happened, not why. "Revenue fell
  18% in February" is a finding; "Revenue fell because of reduced ad spend" is
  an invention — the warehouse holds no ad spend.
- If the rows do not support a conclusion, say so plainly. "The data does not
  show X" is a valid and useful answer.
- Label opinions. A suggestion about what to do next is kind="recommendation",
  never kind="finding".
- List the figures you cite in supporting_values, exactly as they appear.
- Note real limitations in caveats: filters applied, periods excluded, small
  sample sizes.

headline: one sentence that directly answers the question, with the key number.
"""


def build_insight_prompt(
    question: str,
    *,
    sql: str,
    columns: list[str],
    rows: list[tuple[object, ...]],
    row_count: int,
    analysis_summary: str = "",
    max_rows_in_prompt: int = 60,
) -> str:
    """Compose the insight prompt from the actual result set.

    Only the returned rows are included — never the full table and never the
    schema. Grounding is enforced by construction: the model cannot cite a
    figure it was not shown.
    """
    shown = rows[:max_rows_in_prompt]
    header = " | ".join(columns)
    body = "\n".join(" | ".join(_format(value) for value in row) for row in shown)

    sections = [
        f"QUESTION: {question}",
        f"SQL EXECUTED:\n{sql}",
        f"ROW COUNT: {row_count}",
        f"RESULTS:\n{header}\n{'-' * len(header)}\n{body}",
    ]

    if len(rows) > max_rows_in_prompt:
        sections.append(
            f"NOTE: showing the first {max_rows_in_prompt} of {row_count} rows. "
            "Do not make claims about rows you cannot see."
        )

    if analysis_summary:
        sections.append(
            "COMPUTED ANALYSIS (these figures were calculated from the full "
            f"result set and are reliable):\n{analysis_summary}"
        )

    return "\n\n".join(sections)


def _format(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

CHART_SYSTEM = """You choose how to visualise a query result.

You are given the columns, their types, the row count and the question.

Choose the chart that makes the result easiest to read:
- line: a measure over time. Requires a date/period column.
- bar: comparing a measure across categories.
- grouped_bar: a measure across categories, split by a second dimension.
- scatter: the relationship between two numeric measures.
- histogram: the distribution of one numeric column.
- pie: parts of a whole. Only for 2-6 categories that genuinely sum to a total.
- table: everything else, and anything with more than about 30 categories.

Choose "table" when a chart would add nothing. A single-row, single-value result
is a number, not a chart. Many columns of unrelated values is a table.

x_column and y_columns must be exact column names from the list given.
"""


def build_chart_prompt(
    question: str,
    *,
    columns: list[str],
    column_types: list[str],
    row_count: int,
    sample_rows: list[tuple[object, ...]],
) -> str:
    described = "\n".join(
        f"  {name} ({type_name})" for name, type_name in zip(columns, column_types, strict=False)
    )
    sample = "\n".join(" | ".join(_format(value) for value in row) for row in sample_rows[:5])
    return (
        f"QUESTION: {question}\n\n"
        f"COLUMNS:\n{described}\n\n"
        f"ROW COUNT: {row_count}\n\n"
        f"SAMPLE ROWS:\n{sample}"
    )
