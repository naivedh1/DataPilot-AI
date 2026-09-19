"""Column semantics inferred from names.

Shared by the analysis and visualization layers, which both need to know what a
column *means* before deciding what to do with it. It lives in its own module
because each of those imports the other's helpers, and a shared dependency is
cleaner than a cycle.

Column names are the only signal available: PostgreSQL reports NUMERIC for
revenue, a percentage and a count alike. The SQL prompt asks for descriptive
snake_case aliases, which makes these heuristics reliable in practice, and they
only affect presentation and which statistics are reported — never the query or
the underlying figures.
"""

from __future__ import annotations

#: Measures that cannot legitimately be summed across groups. The average of
#: group averages is not the overall average, so "share of total" is meaningless
#: for them and a pie chart — which asserts the slices compose a whole — is
#: actively misleading.
NON_ADDITIVE_TERMS: tuple[str, ...] = (
    "avg",
    "average",
    "mean",
    "median",
    "aov",
    "rate",
    "pct",
    "percent",
    "share",
    "ratio",
    "margin",
    "score",
    "index",
    "per_",
    "_per",
)

_PERCENT_TERMS = ("pct", "percent", "rate", "share", "margin", "ratio")
_CURRENCY_TERMS = (
    "revenue",
    "sales",
    "amount",
    "total",
    "value",
    "price",
    "cost",
    "aov",
    "spend",
    "turnover",
)


def is_additive(column_name: str) -> bool:
    """Whether values in this column can meaningfully be summed across groups."""
    lowered = column_name.lower()
    return not any(term in lowered for term in NON_ADDITIVE_TERMS)


def value_format(column_name: str) -> str:
    """How the frontend should format this column: currency, percent or number."""
    lowered = column_name.lower()
    if any(term in lowered for term in _PERCENT_TERMS):
        return "percent"
    if any(term in lowered for term in _CURRENCY_TERMS):
        return "currency"
    return "number"


def humanize(column_name: str) -> str:
    """A column name as a chart label."""
    return column_name.replace("_", " ").strip().title()
