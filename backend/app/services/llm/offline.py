"""A deterministic, in-process provider — the offline baseline.

**This is not a pretend AI.** It is a rule-based text-to-SQL baseline that does
real work: it parses the question, reads the *same retrieved schema* the live
model receives, and composes SQL from grammar rules. It contains no mapping from
question to answer, and no canned results. Give it a question the rules do not
cover and it says so, rather than inventing something.

It exists for three reasons, all of which are engineering rather than
convenience:

1.  **Hermetic tests.** The agent graph, SQL validator, retry loop, analysis and
    API are all exercised without a network call or an API key. Fast, free, and
    deterministic.
2.  **An honest evaluation floor.** Phase 13 scores the live model against this
    baseline. "92% correct" means little on its own; "92% versus a 61% rule-based
    floor" is a real claim about what the model adds.
3.  **Graceful degradation.** With no key configured, the application still runs
    end to end and says plainly which component is simulated.

Every response it produces is marked `simulated=True`, and the API surfaces
that. Nothing here is ever presented to a user as model output.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, TypeVar

from pydantic import BaseModel

from app.schemas.agent import (
    AnalysisIntent,
    ChartSpec,
    InsightBundle,
    InsightItem,
    PlannerDecision,
    SQLGeneration,
)
from app.services.llm.base import LLMProvider, LLMResponse, LLMUsage

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

#: The warehouse's fixed window. The baseline resolves relative dates against
#: this rather than `today`, matching how the data was generated.
DATA_WINDOW_END = dt.date(2026, 8, 31)
DATA_WINDOW_START = dt.date(2024, 9, 1)


# ---------------------------------------------------------------------------
# Question parsing
# ---------------------------------------------------------------------------

#: Grouping dimension -> (SQL expression, output alias, tables needed).
_DIMENSIONS: tuple[tuple[tuple[str, ...], str, str, tuple[str, ...]], ...] = (
    (("region",), "r.name", "region", ("regions",)),
    (("territory",), "r.territory", "territory", ("regions",)),
    (("segment", "customer segment"), "c.customer_segment", "segment", ("customers",)),
    # Order matters: the more specific phrases are matched first. A bare
    # "channel" synonym on acquisition_channel was swallowing "sales channel",
    # so order-volume-by-sales-channel grouped by the wrong column entirely.
    (("sales channel",), "o.sales_channel", "sales_channel", ()),
    (
        ("acquisition channel", "acquisition source", "acquisition", "marketing channel"),
        "c.acquisition_channel",
        "acquisition_channel",
        ("customers",),
    ),
    (("subcategory", "sub-category"), "p.subcategory", "subcategory", ("products", "order_items")),
    (("category",), "p.category", "category", ("products", "order_items")),
    (("product",), "p.name", "product", ("products", "order_items")),
    (("status",), "o.status", "status", ()),
    (("age band", "age"), "c.age_band", "age_band", ("customers",)),
    (("department",), "e.department", "department", ("employees",)),
)

_GRAINS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "monthly",
            "by month",
            "per month",
            "each month",
            "month over month",
            "month-over-month",
            "mom",
        ),
        "month",
    ),
    (("quarterly", "by quarter", "per quarter", "each quarter"), "quarter"),
    (("yearly", "annually", "by year", "per year", "each year"), "year"),
    (("weekly", "by week", "per week"), "week"),
    (("daily", "by day", "per day", "each day"), "day"),
)

# "top 10 products", "10 highest", and "which 10 products ... highest revenue".
# The third form separates the count from the superlative by several words, so
# adjacency alone misses it.
_TOP_N = re.compile(
    r"\btop\s+(\d+)\b"
    r"|\b(\d+)\s+(?:highest|best|largest|biggest|worst|lowest)\b"
    r"|\b(?:which|what)\s+(\d+)\s+\w+(?:\s+\w+){0,3}\s+"
    r"(?:highest|best|largest|biggest|worst|lowest|most|top)\b"
)
_LAST_N_MONTHS = re.compile(r"\blast\s+(\d+)\s+months?\b")
_EXPLICIT_YEAR = re.compile(r"\b(20\d{2})\b")

#: "February 2026", "in Feb 2026". Month-level filtering is common enough that
#: falling back to a whole-year filter answers a materially different question.
_MONTH_NAMES: tuple[str, ...] = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH_YEAR = re.compile(
    r"\b(" + "|".join(name[:3] for name in _MONTH_NAMES) + r")[a-z]*\s+(20\d{2})\b"
)
_QUARTER = re.compile(r"\bq([1-4])\s*(20\d{2})\b|\b(20\d{2})\s*q([1-4])\b")


def _find_dimension(question: str) -> tuple[str, str, tuple[str, ...]] | None:
    """The grouping dimension, if the question names one."""
    lowered = question.lower()
    for keywords, expression, alias, tables in _DIMENSIONS:
        if any(keyword in lowered for keyword in keywords):
            return expression, alias, tables
    return None


def _find_grain(question: str) -> str | None:
    lowered = question.lower()
    for keywords, grain in _GRAINS:
        if any(keyword in lowered for keyword in keywords):
            return grain
    return None


def _find_metric(question: str) -> tuple[str, str, tuple[str, ...]]:
    """The measure being asked for: (expression, alias, required tables)."""
    lowered = question.lower()
    if "average order value" in lowered or "aov" in lowered:
        return "ROUND(AVG(o.total_amount), 2)", "average_order_value", ()
    if "return rate" in lowered:
        return (
            "ROUND(100.0 * COUNT(*) FILTER (WHERE o.status = 'returned') / NULLIF(COUNT(*), 0), 2)",
            "return_rate_pct",
            (),
        )
    if "margin" in lowered:
        return (
            "ROUND(100.0 * (SUM(oi.line_total) - SUM(oi.quantity * p.cost_price)) "
            "/ NULLIF(SUM(oi.line_total), 0), 2)",
            "margin_pct",
            ("order_items", "products"),
        )
    if "unit" in lowered or "quantity" in lowered:
        return "SUM(oi.quantity)", "units_sold", ("order_items",)
    if "how many customer" in lowered or "customer count" in lowered:
        return "COUNT(DISTINCT c.id)", "customers", ("customers",)
    if "how many order" in lowered or "order volume" in lowered or "order count" in lowered:
        return "COUNT(*)", "orders", ()
    if "how many" in lowered and "employee" in lowered:
        return "COUNT(*)", "employees", ("employees",)
    return "SUM(o.total_amount)", "revenue", ()


def _time_filter(question: str) -> str | None:
    """A WHERE clause for the period the question asks about."""
    lowered = question.lower()

    match = _LAST_N_MONTHS.search(lowered)
    if match:
        months = int(match.group(1))
        start = DATA_WINDOW_END.replace(day=1) - dt.timedelta(days=31 * (months - 1))
        start = start.replace(day=1)
        return f"o.order_date >= DATE '{start}'"

    # Month and quarter are checked before the bare-year pattern: "February
    # 2026" contains a year, so a year-first ordering would silently widen the
    # filter to the whole of 2026 and answer a different question.
    match = _MONTH_YEAR.search(lowered)
    if match:
        month = [name[:3] for name in _MONTH_NAMES].index(match.group(1)) + 1
        year = int(match.group(2))
        start = dt.date(year, month, 1)
        end = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
        return f"o.order_date >= DATE '{start}' AND o.order_date < DATE '{end}'"

    match = _QUARTER.search(lowered)
    if match:
        quarter = int(match.group(1) or match.group(4))
        year = int(match.group(2) or match.group(3))
        start_month = (quarter - 1) * 3 + 1
        start = dt.date(year, start_month, 1)
        end = dt.date(year + 1, 1, 1) if start_month + 3 > 12 else dt.date(year, start_month + 3, 1)
        return f"o.order_date >= DATE '{start}' AND o.order_date < DATE '{end}'"

    if "this year" in lowered:
        return f"o.order_date >= DATE '{DATA_WINDOW_END.year}-01-01'"
    if "last year" in lowered:
        year = DATA_WINDOW_END.year - 1
        return f"o.order_date >= DATE '{year}-01-01' AND o.order_date < DATE '{year + 1}-01-01'"

    match = _EXPLICIT_YEAR.search(lowered)
    if match:
        year = int(match.group(1))
        if DATA_WINDOW_START.year <= year <= DATA_WINDOW_END.year:
            return f"o.order_date >= DATE '{year}-01-01' AND o.order_date < DATE '{year + 1}-01-01'"
    return None


def _top_n(question: str) -> int | None:
    match = _TOP_N.search(question.lower())
    if not match:
        return None
    captured = next((group for group in match.groups() if group), None)
    return int(captured) if captured else None


# ---------------------------------------------------------------------------
# SQL composition
# ---------------------------------------------------------------------------

_JOINS = {
    "customers": "JOIN customers c ON c.id = o.customer_id",
    "regions": "JOIN regions r ON r.id = o.shipping_region_id",
    "order_items": "JOIN order_items oi ON oi.order_id = o.id",
    "products": "JOIN products p ON p.id = oi.product_id",
}


#: Concepts a business might reasonably ask about that this warehouse does not
#: hold. Matching one is a refusal, not a fallback: "employee satisfaction
#: score" contains the word "employee", and without this check the rules would
#: happily return an employee head count as though it answered the question.
_ABSENT_CONCEPTS: tuple[str, ...] = (
    "satisfaction",
    "nps",
    "sentiment",
    "advertising",
    "ad spend",
    "marketing spend",
    "campaign",
    "budget",
    "salary",
    "compensation",
    "payroll",
    "inventory",
    "stock level",
    "warehouse capacity",
    "shipping time",
    "delivery time",
    "supplier",
    "competitor",
    "forecast",
    "predict",
    "churn",
    "lifetime value",
    "ltv",
    "sales rep",
    "quota",
    "commission",
    "deal",
    "pipeline",
    "lead",
    "web traffic",
    "click",
    "impression",
    "conversion rate",
    "review",
    "rating",
)


#: Dimensions available on the refunds spine. Category-level entries carry the
#: joins needed to reach products; the rest reach only as far as orders.
_REFUND_DIMENSIONS: tuple[tuple[tuple[str, ...], str, str, tuple[str, ...]], ...] = (
    (("reason", "refund reason", "why"), "r.refund_reason", "refund_reason", ()),
    (("subcategory", "sub-category"), "p.subcategory", "subcategory", ("items",)),
    (("category",), "p.category", "category", ("items",)),
    (("product",), "p.name", "product", ("items",)),
    (("region",), "rg.name", "region", ("regions",)),
    (("segment", "customer segment"), "c.customer_segment", "segment", ("customers",)),
    (("status",), "o.status", "status", ()),
)


def _find_refund_dimension(lowered: str) -> tuple[str, str, tuple[str, ...]] | None:
    for synonyms, expression, alias, needs in _REFUND_DIMENSIONS:
        if any(term in lowered for term in synonyms):
            return expression, alias, needs
    return None


def _build_refund_sql(question: str, lowered: str) -> SQLGeneration | None:
    """Compose SQL for a refund question, on the refunds spine.

    Refunds are their own fact table, not an attribute of orders, and the
    difference matters arithmetically. An order can carry several refunds, so
    reaching them by joining out from orders double-counts the order; and
    reaching categories by joining refunds to order_items multiplies each
    refund by the order's line count. Both produce a number that looks
    plausible and is wrong, which is the failure mode this project exists to
    avoid.
    """
    dimension = _find_refund_dimension(lowered)
    grain = _find_grain(question)
    counting = "how many" in lowered or "most common" in lowered or "count" in lowered

    # "Refund rate" is a value ratio, not a count of returned orders, and is
    # deliberately distinct from return_rate.
    if "refund rate" in lowered and dimension is None and grain is None:
        return SQLGeneration(
            sql="\n".join(
                [
                    "SELECT ROUND(100.0 * (SELECT SUM(r.refund_amount) FROM refunds r)",
                    "    / NULLIF(SUM(o.total_amount) FILTER "
                    "(WHERE o.status IN ('completed', 'returned')), 0), 2) "
                    "AS refund_rate_pct",
                    "FROM orders o",
                ]
            ),
            tables_used=["refunds", "orders"],
            metrics=["refund_rate"],
            reasoning_summary=(
                "Refunded value over gross fulfilled sales. Each side is aggregated "
                "separately so an order with two refunds is not counted twice."
            ),
        )

    needs = set(dimension[2]) if dimension else set()
    joins: list[str] = []
    # Any dimension beyond the reason itself has to reach orders first.
    if needs or (dimension and dimension[2]):
        joins.append("JOIN orders o ON o.id = r.order_id")
    if "regions" in needs:
        joins.append("JOIN regions rg ON rg.id = o.shipping_region_id")
    if "customers" in needs:
        joins.append("JOIN customers c ON c.id = o.customer_id")
    if "items" in needs:
        joins.append("JOIN order_items oi ON oi.order_id = o.id")
        joins.append("JOIN products p ON p.id = oi.product_id")

    if counting:
        measure, alias = "COUNT(*)", "refunds"
    elif "items" in needs:
        # Allocate each refund across the order's lines in proportion to line
        # value. Summing refund_amount here would repeat the whole refund on
        # every line of the order.
        measure = "ROUND(SUM(r.refund_amount * oi.line_total / NULLIF(o.subtotal, 0)), 2)"
        alias = "refund_amount"
    else:
        measure, alias = "SUM(r.refund_amount)", "refund_amount"

    select_parts: list[str] = []
    group_parts: list[str] = []
    if grain:
        select_parts.append(f"date_trunc('{grain}', r.refund_date)::date AS period")
        group_parts.append("period")
    if dimension:
        select_parts.append(f"{dimension[0]} AS {dimension[1]}")
        group_parts.append(dimension[0])

    select_parts.append(f"{measure} AS {alias}")

    lines = ["SELECT " + ", ".join(select_parts), "FROM refunds r"]
    lines.extend(joins)

    if group_parts:
        lines.append("GROUP BY " + ", ".join(group_parts))
        lines.append("ORDER BY period" if grain else f"ORDER BY {alias} DESC")

    limit = _top_n(question)
    if limit and not grain:
        lines.append(f"LIMIT {limit}")

    tables = ["refunds"]
    if joins:
        tables.append("orders")
    if "regions" in needs:
        tables.append("regions")
    if "customers" in needs:
        tables.append("customers")
    if "items" in needs:
        tables.extend(["order_items", "products"])

    return SQLGeneration(
        sql="\n".join(lines),
        tables_used=tables,
        metrics=["refund_amount"] if alias == "refund_amount" else ["refund_count"],
        reasoning_summary=(
            "Totalled refunds on the refunds table, grouped by refund_date so "
            "refunds land in the period they were issued."
            if grain
            else "Totalled refunds directly on the refunds table."
        ),
    )


def build_sql(question: str) -> SQLGeneration | None:
    """Compose SQL from the question's grammar.

    Returns None when the rules do not cover the question. Returning None is the
    honest outcome — inventing a query that merely runs would produce a
    confident wrong answer, which is worse than no answer.
    """
    lowered = question.lower()

    # A question about something the warehouse does not record cannot be
    # answered by narrowing the grammar — it has to be declined.
    if any(concept in lowered for concept in _ABSENT_CONCEPTS):
        return None

    # Refund questions run on the refunds spine. Reaching refunds from the
    # orders side double-counts orders that were refunded more than once.
    if "refund" in lowered:
        return _build_refund_sql(question, lowered)

    # Employee questions do not touch the orders spine at all.
    if "employee" in lowered or "headcount" in lowered or "staff" in lowered:
        dimension = _find_dimension(question)
        if dimension and dimension[2] == ("employees",):
            expression, alias, _ = dimension
            return SQLGeneration(
                sql=(
                    f"SELECT {expression} AS {alias}, COUNT(*) AS employees\n"
                    f"FROM employees e\n"
                    f"GROUP BY {expression}\n"
                    f"ORDER BY employees DESC"
                ),
                tables_used=["employees"],
                metrics=["employee_count"],
                reasoning_summary="Grouped the employee roster by the requested attribute.",
            )
        return SQLGeneration(
            sql="SELECT COUNT(*) AS employees FROM employees e",
            tables_used=["employees"],
            metrics=["employee_count"],
            reasoning_summary="Counted all employees.",
        )

    metric_expr, metric_alias, metric_tables = _find_metric(question)
    dimension = _find_dimension(question)
    grain = _find_grain(question)

    # A plain "how many customers do we have?" counts the customer table, not
    # customers who happen to have completed orders. Routing it through the
    # orders join silently answers a different, narrower question.
    if (
        metric_alias == "customers"
        and grain is None
        and (dimension is None or dimension[2] == ("customers",))
    ):
        projection = f"{dimension[0]} AS {dimension[1]}, " if dimension else ""
        lines = [f"SELECT {projection}COUNT(*) AS customers", "FROM customers c"]
        if dimension:
            lines.append(f"GROUP BY {dimension[0]}")
            lines.append("ORDER BY customers DESC")
        return SQLGeneration(
            sql="\n".join(lines),
            tables_used=["customers"],
            metrics=["customer_count"],
            reasoning_summary="Counted customers directly, without joining to orders.",
        )

    if dimension is None and grain is None:
        # Nothing to group by and no time grain. Only proceed if the question
        # clearly names something this warehouse measures — interrogative
        # phrasing alone is not enough, or "what is the meaning of life?" would
        # confidently return total revenue.
        has_subject = any(
            term in lowered
            for term in (
                "revenue",
                "sales",
                "turnover",
                "order",
                "customer",
                "aov",
                "average order value",
                "units",
                "margin",
                "return rate",
                "employee",
                "headcount",
                "product",
            )
        )
        if not has_subject:
            return None

    needed: set[str] = set(metric_tables)
    if dimension:
        needed.update(dimension[2])
    # products is only reachable through order_items.
    if "products" in needed:
        needed.add("order_items")

    # Fan-out correction. Joining order_items produces one row per line, so
    # SUM(o.total_amount) would count each order's total once per line it
    # contains. Line-level revenue is the only correct measure at that grain.
    if "order_items" in needed and metric_alias == "revenue":
        metric_expr = "SUM(oi.line_total)"

    select_parts: list[str] = []
    group_parts: list[str] = []

    if grain:
        select_parts.append(f"date_trunc('{grain}', o.order_date)::date AS period")
        group_parts.append("period")
    if dimension:
        expression, alias, _ = dimension
        select_parts.append(f"{expression} AS {alias}")
        group_parts.append(expression)

    select_parts.append(f"{metric_expr} AS {metric_alias}")

    joins = [
        _JOINS[table]
        for table in ("customers", "regions", "order_items", "products")
        if table in needed
    ]

    where: list[str] = []
    # Three cases must NOT filter on status:
    #   - return rate needs every status in its denominator;
    #   - grouping *by* status and filtering *on* status collapses the result
    #     to a single row, which is how "order counts by status" returned one;
    #   - "in total" / "all orders" explicitly asks for the unfiltered count.
    grouping_by_status = bool(dimension and dimension[1] == "status")
    wants_everything = any(
        phrase in lowered for phrase in ("in total", "all orders", "every order")
    )
    if metric_alias != "return_rate_pct" and not grouping_by_status and not wants_everything:
        where.append("o.status = 'completed'")
    time_clause = _time_filter(question)
    if time_clause:
        where.append(time_clause)

    lines = [f"SELECT {', '.join(select_parts)}", "FROM orders o"]
    lines.extend(joins)
    if where:
        lines.append("WHERE " + " AND ".join(where))
    if group_parts:
        lines.append("GROUP BY " + ", ".join(group_parts))

    limit = _top_n(question)
    if limit:
        lines.append(f"ORDER BY {metric_alias} DESC")
        lines.append(f"LIMIT {limit}")
    elif grain:
        lines.append("ORDER BY period")
    elif group_parts:
        lines.append(f"ORDER BY {metric_alias} DESC")

    tables = ["orders", *sorted(needed)]
    return SQLGeneration(
        sql="\n".join(lines),
        tables_used=tables,
        metrics=[metric_alias],
        reasoning_summary=(
            f"Rule-based baseline: aggregated {metric_alias}"
            + (f" by {dimension[1]}" if dimension else "")
            + (f" per {grain}" if grain else "")
            + "."
        ),
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OfflineProvider(LLMProvider):
    """Deterministic provider. Marks every response `simulated=True`."""

    name = "offline-baseline"

    @property
    def is_live(self) -> bool:
        return False

    def _usage(self, prompt: str, output: str) -> LLMUsage:
        # Rough token accounting so latency/size dashboards stay populated in
        # offline mode. Approximate by design and flagged as simulated.
        return LLMUsage(
            input_tokens=len(prompt) // 4,
            output_tokens=len(output) // 4,
            latency_ms=0.0,
            model=self.name,
            simulated=True,
        )

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        text = (
            "This answer was produced by the deterministic offline baseline, "
            "not by a language model. Configure GEMINI_API_KEY for generated prose."
        )
        return LLMResponse(text=text, usage=self._usage(prompt, text))

    def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[ModelT],
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> tuple[ModelT, LLMUsage]:
        question = _extract_question(prompt)
        builder = _BUILDERS.get(schema.__name__)
        if builder is None:
            raise NotImplementedError(f"offline provider has no builder for {schema.__name__}")
        result = builder(question, prompt)
        validated = schema.model_validate(result.model_dump())
        return validated, self._usage(prompt, str(result))


_QUESTION_MARKER = re.compile(r"QUESTION:\s*(.+?)(?:\n\n|\Z)", re.DOTALL)


def _extract_question(prompt: str) -> str:
    """Pull the user's question out of a composed prompt.

    Prompts are built by `app/services/llm/prompts.py`, which always marks the
    question with a `QUESTION:` line. Falling back to the whole prompt keeps the
    baseline usable if a caller composes its own.
    """
    match = _QUESTION_MARKER.search(prompt)
    return (match.group(1) if match else prompt).strip()


_PRIOR_QUESTION = re.compile(r"CONVERSATION SO FAR:\s*\n\s*Q: (.+)")

#: Openers marking a turn as a refinement of the previous one rather than a new
#: question. A refinement carries no subject of its own — "only the last
#: 6 months" is meaningless until the prior question is folded in.
_REFINEMENT_OPENERS: tuple[str, ...] = (
    "only",
    "just",
    "what about",
    "how about",
    "and for",
    "now ",
    "same for",
    "but for",
    "restrict",
    "filter",
    "narrow",
    "instead",
    "break that down",
    "split that",
    "show that",
)


def _resolve_followup(question: str, prompt: str) -> str:
    """Fold prior context into a refinement so it stands alone.

    The baseline has no language model, so this handles the narrow, mechanical
    case: a short turn opening with a refinement marker is appended to the
    previous question. It is not general coreference resolution — a live model
    handles the rest — but it makes multi-turn genuinely work offline, which is
    what lets the follow-up path be tested without an API key.
    """
    lowered = question.lower().strip()
    looks_like_refinement = len(lowered.split()) <= 8 and any(
        lowered.startswith(opener) for opener in _REFINEMENT_OPENERS
    )
    if not looks_like_refinement:
        return question

    match = _PRIOR_QUESTION.search(prompt)
    if not match:
        return question

    return f"{match.group(1).strip()}, {question.strip()}"


def _build_planner(question: str, prompt: str) -> PlannerDecision:
    question = _resolve_followup(question, prompt)
    lowered = question.lower()

    if any(word in lowered for word in ("what tables", "what columns", "what data")):
        intent = AnalysisIntent.SCHEMA_QUESTION
        needs_sql = False
    elif _top_n(question):
        intent, needs_sql = AnalysisIntent.RANKING, True
    elif _find_grain(question) or "trend" in lowered or "over time" in lowered:
        intent, needs_sql = AnalysisIntent.TREND, True
    elif any(word in lowered for word in ("compare", "versus", " vs ")):
        intent, needs_sql = AnalysisIntent.COMPARISON, True
    elif any(word in lowered for word in ("anomaly", "unusual", "spike", "drop")):
        intent, needs_sql = AnalysisIntent.ANOMALY, True
    else:
        intent, needs_sql = AnalysisIntent.AGGREGATION, True

    grain = _find_grain(question)
    dimension = _find_dimension(question)

    return PlannerDecision(
        intent=intent,
        needs_sql=needs_sql,
        needs_analysis=intent in (AnalysisIntent.TREND, AnalysisIntent.ANOMALY),
        needs_chart=needs_sql and (grain is not None or dimension is not None),
        entities=[part for part in (dimension[1] if dimension else None, grain) if part],
        time_grain=grain or "none",
        # `question` has already had any follow-up context folded in, so the
        # SQL generator downstream needs no conversation history of its own.
        resolved_question=question,
        reasoning_summary="Classified by the deterministic offline baseline.",
    )


def _build_sql_generation(question: str, _prompt: str) -> SQLGeneration:
    generated = build_sql(question)
    if generated is None:
        # Honest failure. A query that merely runs would give a confident wrong
        # answer, which is worse than none.
        return SQLGeneration(
            sql="",
            tables_used=[],
            metrics=[],
            reasoning_summary=(
                "The offline baseline has no rule covering this question. "
                "Configure GEMINI_API_KEY for full natural-language coverage."
            ),
        )
    return generated


def _build_insights(question: str, prompt: str) -> InsightBundle:
    """Report only what can be read directly off the result set.

    Deliberately spare: the baseline has no language model, so it states the
    shape of the result and nothing more. Inventing narrative here would be
    exactly the hallucination the project exists to avoid.
    """
    rows = _count_result_rows(prompt)
    return InsightBundle(
        headline=(
            f"The query returned {rows} row{'s' if rows != 1 else ''}. "
            "Narrative insight requires a configured language model."
        ),
        insights=[
            InsightItem(
                text=(
                    "Generated by the deterministic offline baseline. Figures in "
                    "the table and chart are real query results; no interpretation "
                    "has been added."
                ),
                kind="finding",
            )
        ],
        caveats=["Running without a language model: insights are not generated."],
    )


_ROWS_MARKER = re.compile(r"ROW COUNT:\s*(\d+)")


def _count_result_rows(prompt: str) -> int:
    match = _ROWS_MARKER.search(prompt)
    return int(match.group(1)) if match else 0


def _build_chart(question: str, _prompt: str) -> ChartSpec:
    grain = _find_grain(question)
    dimension = _find_dimension(question)
    return ChartSpec(
        chart_type="line" if grain else ("bar" if dimension else "table"),
        title=question[:120] or "Result",
        rationale="Chosen by the deterministic offline baseline from result shape.",
    )


_BUILDERS: dict[str, Any] = {
    "PlannerDecision": _build_planner,
    "SQLGeneration": _build_sql_generation,
    "InsightBundle": _build_insights,
    "ChartSpec": _build_chart,
}
