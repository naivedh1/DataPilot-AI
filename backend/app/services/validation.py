"""Deterministic validation of a query result.

Nothing here asks a language model whether an answer looks right. Every check
is arithmetic the code can perform itself, which is the point: a model that
generated a wrong query is not a reliable judge of whether its own output is
wrong.

The checks fall into two kinds.

**Shape checks** read only the returned rows: a revenue column that went
negative, a percentage above 100, a measure that is entirely NULL, a result
that came back empty. These are cheap and always run.

**Reconciliation** asks the database a second question: does a grouped answer
sum to the same figure as the same measure without the grouping? Revenue by
region should add up to total revenue. When it does not, rows are reaching the
total that never reach the answer.

Its scope is worth stating precisely, because a check that is believed to
cover more than it does is worse than no check. The total is derived from the
grouped query by removing the GROUP BY and its non-aggregate columns, so it
keeps the original FROM, JOIN and WHERE. That means it verifies exactly one
thing: **that the grouped rows partition the same row set the ungrouped
aggregate sees.** It catches a HAVING that quietly drops small groups out of
an answer, and anything else introduced at the grouping layer.

It cannot catch an error in the FROM, JOIN or WHERE, because the derived total
inherits it. A join fan-out inflates both sides equally and reconciles
perfectly. Guarding against that is the semantic layer's job — which is why
the refunds catalogue entry spells out the allocation form — not this
module's.

A check that cannot be applied returns SKIPPED rather than PASSED. Reporting
"passed" for a check that never ran is how a validation layer becomes
decorative.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.services.measures import is_additive

logger = logging.getLogger(__name__)

DIALECT = "postgres"

#: Relative tolerance for reconciliation. Grouped and ungrouped aggregates are
#: computed by the same engine over the same rows, so they agree exactly in
#: principle; this absorbs only the last-place drift that NUMERIC -> float
#: conversion can introduce on very large sums.
RECONCILE_RELATIVE_TOLERANCE = 1e-9

#: Columns whose name marks them as a percentage rather than a bare ratio.
_PERCENT_HINTS = ("pct", "percent", "_rate", "rate_", "share")

#: Measures that cannot legitimately go below zero. A margin can; a count,
#: a refunded amount or a quantity cannot.
_NON_NEGATIVE_HINTS = (
    "count",
    "orders",
    "customers",
    "units",
    "quantity",
    "refund",
    "revenue",
    "sales",
    "amount",
    "total",
)


class CheckStatus(StrEnum):
    """Outcome of a single validation check."""

    PASSED = "passed"
    FAILED = "failed"
    #: The check does not apply to this result. Not a pass.
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    """One named check and what it found."""

    name: str
    status: CheckStatus
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": str(self.status), "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Every check run against one result."""

    checks: tuple[ValidationCheck, ...] = ()

    @property
    def failed(self) -> tuple[ValidationCheck, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.FAILED)

    @property
    def passed(self) -> tuple[ValidationCheck, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.PASSED)

    @property
    def skipped(self) -> tuple[ValidationCheck, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.SKIPPED)

    @property
    def status(self) -> str:
        """`failed` if anything failed, else `passed`.

        A report of nothing but skipped checks is not a pass — there was
        nothing to verify, and saying otherwise would overstate the evidence.
        """
        if self.failed:
            return "failed"
        return "passed" if self.passed else "not_verified"

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "checks": [c.to_dict() for c in self.checks]}


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


def _as_number(value: object) -> float | None:
    """A value as a float, or None when it is not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        # `value != value` is the NaN test; math.isnan would reject Decimal.
        return None if value != value else float(value)
    if isinstance(value, Decimal):
        try:
            return float(value)
        except (ValueError, OverflowError, InvalidOperation):
            return None
    return None


def _numeric_columns(columns: list[str], rows: list[tuple[Any, ...]]) -> dict[str, list[float]]:
    """Column name -> its numeric values, for columns that hold numbers."""
    found: dict[str, list[float]] = {}
    for index, name in enumerate(columns):
        values = [
            number
            for row in rows
            if index < len(row) and (number := _as_number(row[index])) is not None
        ]
        if values:
            found[name] = values
    return found


def _looks_like_percentage(column: str) -> bool:
    lowered = column.lower()
    return any(hint in lowered for hint in _PERCENT_HINTS)


def _must_be_non_negative(column: str) -> bool:
    lowered = column.lower()
    if "margin" in lowered or "change" in lowered or "growth" in lowered or "delta" in lowered:
        # These are differences and may legitimately be negative.
        return False
    return any(hint in lowered for hint in _NON_NEGATIVE_HINTS)


# ---------------------------------------------------------------------------
# Shape checks
# ---------------------------------------------------------------------------


def check_result_not_empty(row_count: int) -> ValidationCheck:
    """An empty result is not an error, but it is not an answer either."""
    if row_count > 0:
        return ValidationCheck("result_not_empty", CheckStatus.PASSED, f"{row_count} rows")
    return ValidationCheck(
        "result_not_empty",
        CheckStatus.FAILED,
        "the query returned no rows, so there is nothing to report",
    )


def check_non_negative_measures(columns: list[str], rows: list[tuple[Any, ...]]) -> ValidationCheck:
    """Counts and money must not be negative."""
    numeric = _numeric_columns(columns, rows)
    applicable = {name: values for name, values in numeric.items() if _must_be_non_negative(name)}
    if not applicable:
        return ValidationCheck(
            "non_negative_measures", CheckStatus.SKIPPED, "no count or amount columns"
        )

    offenders = [
        f"{name}={min(values):,.2f}" for name, values in applicable.items() if min(values) < 0
    ]
    if offenders:
        return ValidationCheck(
            "non_negative_measures", CheckStatus.FAILED, "negative: " + ", ".join(offenders)
        )
    return ValidationCheck(
        "non_negative_measures", CheckStatus.PASSED, f"{len(applicable)} columns checked"
    )


def check_percentages_within_bounds(
    columns: list[str], rows: list[tuple[Any, ...]]
) -> ValidationCheck:
    """A percentage outside 0-100 means the expression is wrong.

    Ratios expressed as fractions (0-1) also pass, since they fall inside the
    same bound. The check is for the order-of-magnitude error — a rate of 4,300
    because the numerator and denominator were swapped, or the x100 applied
    twice.
    """
    numeric = _numeric_columns(columns, rows)
    applicable = {name: values for name, values in numeric.items() if _looks_like_percentage(name)}
    if not applicable:
        return ValidationCheck(
            "percentages_within_bounds", CheckStatus.SKIPPED, "no percentage columns"
        )

    offenders = [
        f"{name} in [{min(values):,.2f}, {max(values):,.2f}]"
        for name, values in applicable.items()
        if min(values) < 0 or max(values) > 100
    ]
    if offenders:
        return ValidationCheck(
            "percentages_within_bounds",
            CheckStatus.FAILED,
            "outside 0-100: " + "; ".join(offenders),
        )
    return ValidationCheck(
        "percentages_within_bounds", CheckStatus.PASSED, f"{len(applicable)} columns checked"
    )


def check_measures_not_all_null(columns: list[str], rows: list[tuple[Any, ...]]) -> ValidationCheck:
    """A measure column that is entirely NULL usually means a broken join.

    The query runs, the shape looks right, and every number is missing. Without
    this the answer reads as "revenue was None", which a narrative layer will
    happily phrase as though it meant zero.
    """
    if not rows:
        return ValidationCheck("measures_not_all_null", CheckStatus.SKIPPED, "no rows")

    numeric_names = set(_numeric_columns(columns, rows))
    all_null = [
        name
        for index, name in enumerate(columns)
        if name not in numeric_names
        and all(index < len(row) and row[index] is None for row in rows)
    ]
    if all_null:
        return ValidationCheck(
            "measures_not_all_null",
            CheckStatus.FAILED,
            "entirely NULL: " + ", ".join(all_null),
        )
    return ValidationCheck("measures_not_all_null", CheckStatus.PASSED, "no all-NULL columns")


def check_not_truncated(truncated: bool, row_count: int) -> ValidationCheck:
    """A capped result set is a partial answer.

    This is a failure rather than a note: any total computed from a truncated
    result is wrong, and the user has no way to see that from the number.
    """
    if not truncated:
        return ValidationCheck("complete_result_set", CheckStatus.PASSED, "no row cap applied")
    return ValidationCheck(
        "complete_result_set",
        CheckStatus.FAILED,
        f"the row cap trimmed the result at {row_count} rows, so totals are partial",
    )


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def build_total_query(sql: str) -> str | None:
    """Rewrite a grouped query as the same aggregates with no grouping.

    `SELECT region, SUM(x) ... GROUP BY region` becomes `SELECT SUM(x) ...`,
    which is the independent total the grouped rows must add up to.

    Returns None when the rewrite would not be trustworthy — no GROUP BY, a
    CTE, a set operation, a LIMIT, or no aggregate to total. Declining is the
    correct outcome there: a reconciliation computed from a query that does not
    mean the same thing is worse than no reconciliation.

    The LIMIT case matters most. A top-N answer is deliberately partial, so its
    rows are not supposed to sum to the whole; reconciling one would report a
    failure that is the query working as intended. Note this must be given the
    SQL *before* the validator applies its row cap, or every query looks like
    a top-N.

    HAVING is dropped rather than preserved, on purpose. Keeping it would make
    both sides exclude the same groups and the check would always pass; the
    point is to surface groups the answer omits but the total includes.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=DIALECT)
    except ParseError:
        return None

    if not isinstance(tree, exp.Select):
        return None
    if tree.args.get("with") or tree.args.get("distinct"):
        return None
    if tree.args.get("limit") or tree.args.get("offset"):
        # A top-N result is deliberately partial; its parts cannot sum to the
        # whole, and flagging that as a failure would be wrong.
        return None
    if not tree.args.get("group"):
        return None

    aggregates = [column for column in tree.expressions if column.find(exp.AggFunc)]
    if not aggregates:
        return None

    total = tree.copy()
    total.set("expressions", [column.copy() for column in aggregates])
    total.set("group", None)
    total.set("order", None)
    total.set("having", None)
    return total.sql(dialect=DIALECT)


def _matches(grouped_sum: float, total: float) -> bool:
    scale = max(abs(grouped_sum), abs(total), 1.0)
    return abs(grouped_sum - total) <= RECONCILE_RELATIVE_TOLERANCE * scale


def check_group_reconciliation(
    columns: list[str],
    rows: list[tuple[Any, ...]],
    total_columns: list[str],
    total_rows: list[tuple[Any, ...]],
) -> ValidationCheck:
    """Do the grouped rows add up to the ungrouped total?

    Only additive measures are compared. An average of group averages is not
    the overall average, so reconciling one would report a failure that is
    arithmetic rather than a defect.

    A pass means the answer's rows cover the same data the total does. It does
    not mean the underlying query is correct — see the module docstring for
    what this check can and cannot see.
    """
    if not rows or not total_rows:
        return ValidationCheck("group_reconciliation", CheckStatus.SKIPPED, "nothing to compare")

    grouped = _numeric_columns(columns, rows)
    totals = _numeric_columns(total_columns, total_rows)

    compared: list[str] = []
    mismatched: list[str] = []
    for name, values in grouped.items():
        if not is_additive(name) or name not in totals:
            continue
        expected = totals[name][0]
        actual = sum(values)
        compared.append(name)
        if not _matches(actual, expected):
            drift = actual - expected
            mismatched.append(
                f"{name}: parts {actual:,.2f} vs total {expected:,.2f} ({drift:+,.2f})"
            )

    if not compared:
        return ValidationCheck(
            "group_reconciliation", CheckStatus.SKIPPED, "no additive measure to reconcile"
        )
    if mismatched:
        return ValidationCheck("group_reconciliation", CheckStatus.FAILED, "; ".join(mismatched))
    return ValidationCheck(
        "group_reconciliation",
        CheckStatus.PASSED,
        f"parts sum to the total for {', '.join(compared)}",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def validate_result(
    *,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    row_count: int,
    truncated: bool,
    total_columns: list[str] | None = None,
    total_rows: list[tuple[Any, ...]] | None = None,
) -> ValidationReport:
    """Run every applicable check over one result.

    `total_columns`/`total_rows` come from executing `build_total_query`. When
    they are absent the reconciliation check is skipped rather than assumed to
    pass.
    """
    checks = [
        check_result_not_empty(row_count),
        check_non_negative_measures(columns, rows),
        check_percentages_within_bounds(columns, rows),
        check_measures_not_all_null(columns, rows),
        check_not_truncated(truncated, row_count),
    ]

    if total_columns is not None and total_rows is not None:
        checks.append(check_group_reconciliation(columns, rows, total_columns, total_rows))
    else:
        checks.append(
            ValidationCheck(
                "group_reconciliation",
                CheckStatus.SKIPPED,
                "not a groupable aggregate, or the total query could not be derived",
            )
        )

    return ValidationReport(tuple(checks))
