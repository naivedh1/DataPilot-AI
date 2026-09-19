"""Relevant-schema retrieval.

Sending the whole warehouse to the model on every question is the default
mistake in text-to-SQL systems. Six tables is small enough to get away with it
here — which is precisely why doing it properly matters: the technique has to be
in place before it is forced, or the system does not scale past a toy.

The approach is **lexical scoring, not embeddings**. That is a deliberate choice:

* it is deterministic, so the same question always retrieves the same schema and
  the evaluation suite measures the model rather than the retriever's mood;
* it needs no embedding call, so retrieval adds no latency and no cost;
* it is debuggable — every score can be explained by the terms that produced it.

Embeddings become the right answer when table count outgrows a curated synonym
list. The `SchemaRetriever` interface is what would be swapped; nothing above it
would change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.schema.catalog import METRICS, MetricDoc, join_clause
from app.services.schema.introspect import TableInfo, get_schema

#: Words carrying no analytical signal. Kept deliberately short: an aggressive
#: stop list removes terms like "top" or "by" that genuinely shape a query.
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "for",
        "to",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "what",
        "which",
        "who",
        "how",
        "show",
        "me",
        "give",
        "tell",
        "get",
        "find",
        "list",
        "our",
        "we",
        "us",
        "i",
        "my",
        "do",
        "does",
        "did",
        "can",
        "could",
        "would",
        "please",
        "there",
        "that",
        "this",
        "it",
        "its",
        "with",
        "from",
        "at",
        "as",
        "by",
    }
)

_WORD = re.compile(r"[a-z0-9_]+")

# Scoring weights. Tuned so an exact table-name mention always outranks an
# incidental column-synonym match.
_SCORE_TABLE_NAME = 10.0
_SCORE_TABLE_SYNONYM = 6.0
_SCORE_COLUMN_NAME = 3.0
_SCORE_COLUMN_SYNONYM = 2.0
_SCORE_ALLOWED_VALUE = 5.0
_SCORE_METRIC = 4.0

#: Any table scoring at least this is considered relevant.
RELEVANCE_THRESHOLD = 2.0

#: `orders` is the spine of the warehouse. If a question matched a table that
#: can only be reached through it, it has to come along.
_REQUIRES_ORDERS = frozenset({"order_items"})


@dataclass(frozen=True, slots=True)
class RetrievedSchema:
    """The schema subset selected for one question."""

    tables: tuple[TableInfo, ...]
    metrics: tuple[MetricDoc, ...]
    joins: tuple[str, ...]
    #: Score per table, for logging and for explaining a retrieval.
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(table.name for table in self.tables)

    def render(self) -> str:
        """The schema block inserted into the SQL-generation prompt."""
        sections = [table.render() for table in self.tables]

        if self.joins:
            sections.append("JOIN PATHS\n" + "\n".join(f"  {clause}" for clause in self.joins))

        if self.metrics:
            lines = ["METRIC DEFINITIONS (use these exact definitions)"]
            for metric in self.metrics:
                lines.append(f"  {metric.name}: {metric.expression}")
                lines.append(f"    {metric.description}")
                if metric.caveat:
                    lines.append(f"    CAVEAT: {metric.caveat}")
            sections.append("\n".join(lines))

        return "\n\n".join(sections)


def tokenize(question: str) -> set[str]:
    """Lowercase word tokens, stopwords removed.

    Also emits singular forms, so "products" matches the `products` table and
    "customers" matches `customers` without needing both spellings everywhere.
    """
    words = _WORD.findall(question.lower())
    tokens: set[str] = set()
    for word in words:
        if word in STOPWORDS or len(word) < 2:
            continue
        tokens.add(word)
        if word.endswith("ies") and len(word) > 4:
            tokens.add(word[:-3] + "y")
        elif word.endswith("es") and len(word) > 3:
            tokens.add(word[:-2])
        if word.endswith("s") and len(word) > 3:
            tokens.add(word[:-1])
    return tokens


def _phrase_hits(question: str, phrases: tuple[str, ...]) -> int:
    """Count multi-word synonyms present in the raw question.

    Token matching alone cannot see "average order value" as one thing, and
    that phrase is exactly the kind of term a metric is keyed on.
    """
    lowered = question.lower()
    return sum(1 for phrase in phrases if " " in phrase and phrase in lowered)


def score_tables(question: str) -> dict[str, float]:
    """Score every table for relevance to a question."""
    tokens = tokenize(question)
    lowered = question.lower()
    scores: dict[str, float] = {}

    for name, table in get_schema().items():
        score = 0.0

        if name in tokens or name.replace("_", " ") in lowered:
            score += _SCORE_TABLE_NAME
        score += _SCORE_TABLE_SYNONYM * sum(1 for synonym in table.synonyms if synonym in tokens)
        score += _SCORE_TABLE_SYNONYM * _phrase_hits(lowered, table.synonyms)

        for column in table.columns:
            if column.name in tokens:
                score += _SCORE_COLUMN_NAME
            score += _SCORE_COLUMN_SYNONYM * sum(
                1 for synonym in column.synonyms if synonym in tokens
            )
            score += _SCORE_COLUMN_SYNONYM * _phrase_hits(lowered, column.synonyms)
            # A literal from the data ("Enterprise", "cancelled") is a strong
            # signal: it can only have come from this column's vocabulary.
            score += _SCORE_ALLOWED_VALUE * sum(
                1 for value in column.allowed_values if value.lower() in lowered
            )

        scores[name] = score

    return scores


def match_metrics(question: str) -> tuple[MetricDoc, ...]:
    """Metrics whose name or synonyms appear in the question."""
    tokens = tokenize(question)
    lowered = question.lower()
    matched = [
        metric
        for metric in METRICS
        if metric.name in tokens
        or any(synonym in tokens for synonym in metric.synonyms)
        or any(" " in synonym and synonym in lowered for synonym in metric.synonyms)
    ]
    return tuple(matched)


def _resolve_joins(table_names: set[str]) -> tuple[str, ...]:
    """Join clauses connecting the selected tables, in a stable order."""
    ordered = sorted(table_names)
    clauses: list[str] = []
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            clause = join_clause(left, right)
            if clause:
                clauses.append(clause)
    return tuple(clauses)


def retrieve(
    question: str,
    *,
    max_tables: int = 4,
    extra_tables: tuple[str, ...] = (),
) -> RetrievedSchema:
    """Select the schema subset relevant to a question.

    `extra_tables` lets the planner force tables in — used by follow-up turns,
    where the previous query's tables remain relevant even if this turn's
    wording no longer mentions them.
    """
    schema = get_schema()
    scores = score_tables(question)
    metrics = match_metrics(question)

    selected = {name for name, score in scores.items() if score >= RELEVANCE_THRESHOLD}

    # Metrics name the tables they need; a question mentioning "margin" needs
    # products even if it never says "product".
    for metric in metrics:
        selected.update(metric.required_tables)

    selected.update(name for name in extra_tables if name in schema)

    # Nothing matched — fall back to the transactional core rather than to the
    # whole warehouse or to nothing at all.
    if not selected:
        selected = {"orders", "customers"}

    # Keep the highest-scoring tables, but never drop one a metric requires.
    required = {t for metric in metrics for t in metric.required_tables}
    required.update(extra_tables)
    if len(selected) > max_tables:
        ranked = sorted(selected, key=lambda name: (-scores.get(name, 0.0), name))
        selected = set(ranked[:max_tables]) | required

    # `order_items` is only reachable through `orders`.
    if selected & _REQUIRES_ORDERS:
        selected.add("orders")

    tables = tuple(
        schema[name] for name in sorted(selected, key=lambda n: (-scores.get(n, 0.0), n))
    )
    return RetrievedSchema(
        tables=tables,
        metrics=metrics,
        joins=_resolve_joins(selected),
        scores={name: scores.get(name, 0.0) for name in selected},
    )


def render_full_schema() -> str:
    """Every table, for the `/api/schema` endpoint and for debugging."""
    schema = get_schema()
    return "\n\n".join(schema[name].render() for name in sorted(schema))
