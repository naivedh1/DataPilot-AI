"""Multi-step diagnostic investigation.

"Why did revenue fall in August?" is not a query. It is a small argument:
establish that revenue fell, decide what to compare against, break the change
down by the dimensions that could explain it, check the mechanisms that could
have caused it, and confirm the parts add up. One SELECT cannot make that
argument, and asking a model to emit six of them freehand produces six
opportunities to get an aggregate subtly wrong.

**Who decides what, and why.** The brief gives the model "decide what
analytical investigation to perform next" and gives deterministic code "metric
calculations when definitions are known". Those pull in opposite directions
here, and this module resolves the tension by splitting the decision from the
arithmetic:

* The **model** reads intent — that this is a diagnostic question, which
  metric it is about, which periods to compare, which dimensions are worth
  investigating. That is language understanding, which it is good at.
* **This module** owns every figure. The SQL comes from templates below, not
  from the model, so a contribution breakdown cannot fan out across order
  lines, cannot silently drop a status filter, and cannot compare a month
  against a quarter.

The cost is real and worth stating: the investigation can only follow shapes
that exist here. A question needing a genuinely novel decomposition falls back
to the single-query path. The gain is that when it does run, every number in
the evidence trail was computed by code that is tested, rather than by a model
that was fluent.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

#: The loaded history. Diagnostic periods are resolved against this, never
#: against the wall clock — see `seed/config.py` for why the window is pinned.
WINDOW_START = dt.date(2024, 9, 1)
WINDOW_END = dt.date(2026, 8, 31)


# ---------------------------------------------------------------------------
# What can be investigated
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """A measure the investigation can decompose.

    `measure` is a bare aggregate with no FILTER: the period filters are
    applied by the templates, and an aggregate that carried its own filter
    could not then be sliced by period.
    """

    name: str
    measure: str
    from_clause: str
    date_column: str
    base_where: str = ""
    #: The measure to use when the query must descend to order lines. Summing
    #: an order total across its lines multiplies it by the line count.
    line_level_measure: str = ""
    #: Whether the line-level measure sums cleanly across a line-grain split.
    #: COUNT(DISTINCT order) does not: an order spanning two categories is
    #: counted in both, so the parts exceed the whole by design.
    line_level_additive: bool = True
    #: What the line-level measure leaves out relative to the headline, stated
    #: for the caveat. Empty when the two are the same quantity.
    line_level_caveat: str = ""
    unit: str = "currency"


METRICS: dict[str, MetricSpec] = {
    "revenue": MetricSpec(
        name="revenue",
        measure="SUM(o.total_amount)",
        from_clause="FROM orders o",
        date_column="o.order_date",
        base_where="o.status = 'completed'",
        line_level_measure="SUM(oi.line_total)",
        line_level_caveat=(
            "Splitting revenue by product or category uses line values, which "
            "exclude tax and shipping and do not carry order-level discounts. "
            "Those components cannot be attributed to a single product, so the "
            "breakdown totals less than headline revenue."
        ),
    ),
    "orders": MetricSpec(
        name="orders",
        measure="COUNT(DISTINCT o.id)",
        from_clause="FROM orders o",
        date_column="o.order_date",
        base_where="o.status = 'completed'",
        line_level_measure="COUNT(DISTINCT o.id)",
        line_level_additive=False,
        line_level_caveat=(
            "An order containing several categories is counted once in each, "
            "so a category split of order counts sums to more than the total."
        ),
        unit="count",
    ),
    "units": MetricSpec(
        name="units",
        measure="SUM(oi.quantity)",
        from_clause="FROM orders o JOIN order_items oi ON oi.order_id = o.id",
        date_column="o.order_date",
        base_where="o.status = 'completed'",
        line_level_measure="SUM(oi.quantity)",
        unit="count",
    ),
    "refunds": MetricSpec(
        name="refunds",
        measure="SUM(r.refund_amount)",
        from_clause="FROM refunds r JOIN orders o ON o.id = r.order_id",
        # Refunds belong to the period they were issued in, not the period of
        # the sale they reverse.
        date_column="r.refund_date",
        line_level_measure="",
    ),
}


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    """A way to slice a metric."""

    name: str
    expression: str
    #: Extra joins this dimension needs, appended to the metric's FROM.
    joins: tuple[str, ...] = ()
    #: True when reaching it requires descending to order lines, which forces
    #: the line-level measure.
    line_level: bool = False


DIMENSIONS: dict[str, DimensionSpec] = {
    "region": DimensionSpec(
        "region",
        "rg.name",
        ("JOIN regions rg ON rg.id = o.shipping_region_id",),
    ),
    "category": DimensionSpec(
        "category",
        "p.category",
        (
            "JOIN order_items oi ON oi.order_id = o.id",
            "JOIN products p ON p.id = oi.product_id",
        ),
        line_level=True,
    ),
    "product": DimensionSpec(
        "product",
        "p.name",
        (
            "JOIN order_items oi ON oi.order_id = o.id",
            "JOIN products p ON p.id = oi.product_id",
        ),
        line_level=True,
    ),
    "segment": DimensionSpec(
        "segment",
        "c.customer_segment",
        ("JOIN customers c ON c.id = o.customer_id",),
    ),
    "channel": DimensionSpec("channel", "o.sales_channel"),
}


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Period:
    """A half-open date range: start inclusive, end exclusive."""

    start: dt.date
    end: dt.date
    label: str

    def clause(self, column: str) -> str:
        return f"{column} >= DATE '{self.start}' AND {column} < DATE '{self.end}'"

    @property
    def available(self) -> bool:
        """Whether the warehouse holds data for this period at all."""
        return self.start <= WINDOW_END and self.end > WINDOW_START

    @property
    def display(self) -> str:
        """The period as a person would write it: "February 2026".

        `label` stays machine-readable (YYYY-MM) for the plan payload; this is
        what goes into a sentence.
        """
        return self.start.strftime("%B %Y")


def month_period(year: int, month: int) -> Period:
    start = dt.date(year, month, 1)
    end = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    return Period(start, end, f"{year}-{month:02d}")


def parse_period(text: str) -> Period | None:
    """Parse `YYYY-MM`. Returns None for anything else."""
    parts = text.strip().split("-")
    if len(parts) != 2:
        return None
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not 1 <= month <= 12 or not 2000 <= year <= 2100:
        return None
    return month_period(year, month)


def preceding_month(period: Period) -> Period:
    """The month before `period`. The default comparison for a month."""
    if period.start.month == 1:
        return month_period(period.start.year - 1, 12)
    return month_period(period.start.year, period.start.month - 1)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvestigationPlan:
    """What to investigate. Every field is validated against the tables above."""

    metric: str
    current: Period
    comparison: Period
    dimensions: tuple[str, ...]
    checks: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": "diagnostic",
            "metric": self.metric,
            "current_period": self.current.label,
            "comparison_period": self.comparison.label,
            "current_period_display": self.current.display,
            "comparison_period_display": self.comparison.display,
            "dimensions_to_investigate": list(self.dimensions),
            "checks": list(self.checks),
        }


def build_plan(
    *,
    metric: str,
    current_period: str,
    comparison_period: str = "",
    dimensions: list[str] | None = None,
) -> InvestigationPlan | None:
    """Validate a model-proposed plan into one this module can execute.

    Returns None when the plan names something unsupported. Declining is the
    point: an investigation assembled from a metric that does not exist would
    produce an evidence trail of confident nonsense.
    """
    if metric not in METRICS:
        logger.info("unsupported diagnostic metric: %s", metric)
        return None

    current = parse_period(current_period)
    if current is None or not current.available:
        logger.info("unusable current period: %s", current_period)
        return None

    comparison = parse_period(comparison_period) if comparison_period else None
    if comparison is None:
        comparison = preceding_month(current)
    if not comparison.available:
        logger.info("comparison period outside the window: %s", comparison.label)
        return None

    requested = dimensions or ["region", "category"]
    valid = tuple(name for name in requested if name in DIMENSIONS)
    if not valid:
        valid = ("region",)

    checks = ["reconciliation"]
    if metric == "revenue":
        checks.append("refund_impact")
        checks.append("discount_impact")

    return InvestigationPlan(metric, current, comparison, valid, tuple(checks))


# ---------------------------------------------------------------------------
# SQL templates
# ---------------------------------------------------------------------------


def _period_columns(spec: MetricSpec, measure: str, plan: InvestigationPlan) -> str:
    current = plan.current.clause(spec.date_column)
    comparison = plan.comparison.clause(spec.date_column)
    return (
        f"COALESCE({measure} FILTER (WHERE {current}), 0) AS current_value,\n"
        f"       COALESCE({measure} FILTER (WHERE {comparison}), 0) AS previous_value,\n"
        f"       COALESCE({measure} FILTER (WHERE {current}), 0)\n"
        f"         - COALESCE({measure} FILTER (WHERE {comparison}), 0) AS change"
    )


def _window_where(spec: MetricSpec, plan: InvestigationPlan) -> str:
    """Restrict to the two periods, which may not be adjacent."""
    both = (
        f"(({plan.current.clause(spec.date_column)}) "
        f"OR ({plan.comparison.clause(spec.date_column)}))"
    )
    return f"{spec.base_where} AND {both}" if spec.base_where else both


def build_totals_sql(plan: InvestigationPlan, *, line_level: bool = False) -> str:
    """The headline: the metric in both periods, and the change between them.

    `line_level` builds the same figure at order-line grain. That is a
    different quantity — for revenue it drops tax, shipping and order-level
    discounts — and exists so a category or product breakdown can be
    reconciled against a total computed the same way it was.
    """
    spec = METRICS[plan.metric]
    if not line_level:
        return (
            f"SELECT {_period_columns(spec, spec.measure, plan)}\n"
            f"{spec.from_clause}\n"
            f"WHERE {_window_where(spec, plan)}"
        )

    return (
        f"SELECT {_period_columns(spec, spec.line_level_measure, plan)}\n"
        f"{spec.from_clause}\n"
        f"JOIN order_items oi ON oi.order_id = o.id\n"
        f"WHERE {_window_where(spec, plan)}"
    )


def build_contribution_sql(plan: InvestigationPlan, dimension: str) -> str:
    """The same change, split by one dimension and ranked by contribution."""
    spec = METRICS[plan.metric]
    dim = DIMENSIONS[dimension]

    measure = spec.measure
    if dim.line_level and spec.line_level_measure:
        # Descending to order lines multiplies an order-level total by the
        # number of lines. The line-level measure is the only correct one.
        measure = spec.line_level_measure

    joins = "\n".join(dim.joins)
    from_clause = f"{spec.from_clause}\n{joins}" if joins else spec.from_clause

    return (
        f"SELECT {dim.expression} AS {dim.name},\n"
        f"       {_period_columns(spec, measure, plan)}\n"
        f"{from_clause}\n"
        f"WHERE {_window_where(spec, plan)}\n"
        f"GROUP BY {dim.expression}\n"
        f"ORDER BY change ASC"
    )


def build_refund_impact_sql(plan: InvestigationPlan) -> str:
    """Refunds issued in each period. A rise here depresses net revenue."""
    spec = METRICS["refunds"]
    return (
        f"SELECT {_period_columns(spec, spec.measure, plan)}\n"
        f"{spec.from_clause}\n"
        f"WHERE {_window_where(spec, plan)}"
    )


def build_discount_impact_sql(plan: InvestigationPlan) -> str:
    """Discount given away in each period."""
    spec = METRICS["revenue"]
    measure = "SUM(o.discount_amount)"
    return (
        f"SELECT {_period_columns(spec, measure, plan)}\n"
        f"{spec.from_clause}\n"
        f"WHERE {_window_where(spec, plan)}"
    )


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Step:
    """One executed query and what it was for."""

    name: str
    purpose: str
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "sql": self.sql,
            "columns": self.columns,
            "rows": [list(row) for row in self.rows],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class Contributor:
    """One dimension value's share of the overall change."""

    dimension: str
    label: str
    change: float
    share_of_change: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "label": self.label,
            "change": round(self.change, 2),
            "share_of_change": round(self.share_of_change, 4),
        }


@dataclass(frozen=True, slots=True)
class InvestigationResult:
    """Everything the investigation established."""

    plan: InvestigationPlan
    steps: tuple[Step, ...]
    current_value: float = 0.0
    previous_value: float = 0.0
    change: float = 0.0
    percent_change: float | None = None
    contributors: tuple[Contributor, ...] = ()
    reconciled: bool | None = None
    caveats: tuple[str, ...] = ()

    @property
    def direction(self) -> str:
        if self.change < 0:
            return "fell"
        if self.change > 0:
            return "rose"
        return "was flat"

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "current_value": round(self.current_value, 2),
            "previous_value": round(self.previous_value, 2),
            "change": round(self.change, 2),
            "percent_change": (
                None if self.percent_change is None else round(self.percent_change, 2)
            ),
            "contributors": [c.to_dict() for c in self.contributors],
            "reconciled": self.reconciled,
            "caveats": list(self.caveats),
        }


#: Signature of the executor the investigation is given. Injected rather than
#: imported so the templates can be tested without a database.
Executor = Callable[[str], tuple[list[str], list[tuple[Any, ...]]]]


def _number(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _run(executor: Executor, name: str, purpose: str, sql: str) -> Step:
    try:
        columns, rows = executor(sql)
    except Exception as error:
        logger.info("investigation step %s failed: %s", name, error)
        return Step(name, purpose, sql, error=str(error)[:300])
    return Step(name, purpose, sql, columns=columns, rows=rows)


def run_investigation(plan: InvestigationPlan, executor: Executor) -> InvestigationResult:
    """Execute the plan and compute every figure from the returned rows.

    Nothing here is narrated. The result is arithmetic; turning it into prose
    is the insight node's job, and it is given these numbers rather than asked
    to derive them.
    """
    spec = METRICS[plan.metric]
    steps: list[Step] = []
    caveats: list[str] = []

    totals = _run(
        executor,
        "period_totals",
        f"{plan.metric} in {plan.current.label} against {plan.comparison.label}",
        build_totals_sql(plan),
    )
    steps.append(totals)

    current_value = previous_value = change = 0.0
    if totals.rows:
        row = totals.rows[0]
        current_value, previous_value = _number(row[0]), _number(row[1])
        change = _number(row[2])

    percent_change = (change / previous_value * 100.0) if previous_value else None

    totals_by_grain: dict[str, float] = {"order": change}
    line_level_steps: set[str] = set()
    skip: set[str] = set()

    uses_line_grain = any(DIMENSIONS[name].line_level for name in plan.dimensions)
    if uses_line_grain and spec.line_level_measure:
        line_totals = _run(
            executor,
            "line_level_totals",
            "the same change measured at order-line grain, for reconciliation",
            build_totals_sql(plan, line_level=True),
        )
        steps.append(line_totals)
        if line_totals.rows and not line_totals.error:
            totals_by_grain["line"] = _number(line_totals.rows[0][2])
        if spec.line_level_caveat:
            caveats.append(spec.line_level_caveat)

    contributors: list[Contributor] = []
    for dimension in plan.dimensions:
        dim = DIMENSIONS[dimension]
        step_name = f"contribution_by_{dimension}"
        step = _run(
            executor,
            step_name,
            f"which {dimension} accounts for the change",
            build_contribution_sql(plan, dimension),
        )
        steps.append(step)

        if dim.line_level:
            line_level_steps.add(step_name)
            if not spec.line_level_additive:
                skip.add(step_name)

        # Contributions are shares of the change at the step's own grain, so
        # a line-level split is measured against the line-level change.
        basis = totals_by_grain.get("line" if dim.line_level else "order", change)
        contributors.extend(_contributors(dimension, step, basis))

    if "refund_impact" in plan.checks:
        steps.append(
            _run(
                executor,
                "refund_impact",
                "whether refunds moved between the periods",
                build_refund_impact_sql(plan),
            )
        )
    if "discount_impact" in plan.checks:
        steps.append(
            _run(
                executor,
                "discount_impact",
                "whether discounting moved between the periods",
                build_discount_impact_sql(plan),
            )
        )

    reconciled = _reconciles(steps, totals_by_grain, line_level_steps, skip)
    if reconciled is False:
        caveats.append(
            "The dimension breakdowns do not sum to the headline change, so "
            "the attribution below is incomplete."
        )

    # Largest absolute movers first, across every dimension examined.
    contributors.sort(key=lambda c: abs(c.change), reverse=True)

    return InvestigationResult(
        plan=plan,
        steps=tuple(steps),
        current_value=current_value,
        previous_value=previous_value,
        change=change,
        percent_change=percent_change,
        contributors=tuple(contributors[:8]),
        reconciled=reconciled,
        caveats=tuple(caveats),
    )


def _contributors(dimension: str, step: Step, total_change: float) -> list[Contributor]:
    """Each dimension value's change, as a share of the overall change.

    Share is signed against the total: a group moving the same way as the
    headline gets a positive share, one moving against it a negative share.
    Both matter — "North fell but South grew" is the shape of most real
    diagnoses.
    """
    if step.error or not step.rows or not total_change:
        return []

    found: list[Contributor] = []
    for row in step.rows:
        if len(row) < 4:
            continue
        group_change = _number(row[3])
        if group_change == 0.0:
            continue
        found.append(
            Contributor(
                dimension=dimension,
                label=str(row[0]),
                change=group_change,
                share_of_change=group_change / total_change,
            )
        )
    return found


def _reconciles(
    steps: list[Step],
    totals_by_grain: dict[str, float],
    line_level_steps: set[str],
    skip: set[str],
) -> bool | None:
    """Do the breakdowns sum back to a total computed the same way?

    Each contribution step is compared against the headline for *its own*
    grain. An order-level split reconciles against order-level revenue; a
    category split reconciles against line-level revenue, because line values
    exclude tax, shipping and order-level discounts and therefore cannot equal
    the order-level figure. Comparing the two would report that arithmetic as
    a defect — which the first version of this module did.

    Steps in `skip` are not reconcilable at all (a COUNT(DISTINCT order) split
    by category counts an order in each of its categories) and are excluded
    rather than failed.

    Returns None when nothing could be compared.
    """
    checked = False
    for step in steps:
        if not step.name.startswith("contribution_by_") or step.error or not step.rows:
            continue
        if step.name in skip:
            continue

        grain = "line" if step.name in line_level_steps else "order"
        expected = totals_by_grain.get(grain)
        if expected is None or not expected:
            continue

        summed = sum(_number(row[3]) for row in step.rows if len(row) >= 4)
        checked = True
        if abs(summed - expected) > max(abs(expected), 1.0) * 1e-6:
            logger.info(
                "%s does not reconcile: parts %.2f vs %s-level total %.2f",
                step.name,
                summed,
                grain,
                expected,
            )
            return False
    return True if checked else None
