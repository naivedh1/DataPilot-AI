"""Deterministic data profiling.

What the warehouse actually contains, measured rather than described. Row
counts, null rates, cardinality, ranges, duplicate candidates, orphaned
foreign keys, numeric outliers.

**No part of this is asked of a language model.** A model given a table can
produce a confident, plausible profile without reading a single row, and there
is no way to tell that profile from a real one by looking at it. Every figure
here comes from a query.

Two design choices worth knowing:

*One query per table, not per column.* A twelve-column table profiled
column-by-column is twelve round trips and twelve sequential scans. The
aggregate is built as a single SELECT with one expression per column per
statistic, so the table is read once.

*Structure comes from the model layer, not from guessing.* Primary and foreign
keys are declared in `app/models`, so this does not infer them from column
names. What it does instead is check whether the declared constraints are
actually honoured by the data — a foreign key with orphaned rows is a real
finding, where a guessed key is noise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.database.executor import execute_readonly
from app.services.schema.introspect import ColumnInfo, TableInfo, get_schema

logger = logging.getLogger(__name__)

#: Columns whose distinct-value count is at most this are summarised by their
#: actual values rather than by a cardinality number. Below it a list is more
#: informative; above it, it is a wall of text.
MAX_CATEGORICAL_VALUES = 12

#: Outliers are flagged beyond this many interquartile ranges from the median.
#: 3.0 rather than the conventional 1.5: at 1.5 a right-skewed revenue column
#: reports a tenth of its rows as outliers, which is a distribution, not a
#: defect.
OUTLIER_IQR_MULTIPLIER = 3.0

#: Above this share of rows, "outliers" are not outliers — they are the shape
#: of the distribution. A zero-inflated column like `discount_amount` has
#: Q1 = 0 and a tiny IQR, so every discounted row falls outside the fence;
#: reporting 22% of a table as anomalous teaches the reader to ignore the
#: finding.
MAX_OUTLIER_SHARE = 0.05

#: Audit columns, excluded from findings. They are written by the database on
#: insert, so in a bulk-loaded warehouse they are constant by construction —
#: a fact about the load, not about the data.
AUDIT_COLUMNS = ("created_at", "updated_at")


@dataclass(frozen=True, slots=True)
class ColumnProfile:
    """What one column contains."""

    name: str
    type_name: str
    nullable: bool
    null_count: int
    null_percent: float
    distinct_count: int
    #: Distinct values over non-null rows. 1.0 means every value is unique.
    cardinality_ratio: float
    minimum: Any = None
    maximum: Any = None
    mean: float | None = None
    #: Present only for low-cardinality columns.
    top_values: tuple[tuple[str, int], ...] = ()
    outlier_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type_name,
            "nullable": self.nullable,
            "null_count": self.null_count,
            "null_percent": round(self.null_percent, 2),
            "distinct_count": self.distinct_count,
            "cardinality_ratio": round(self.cardinality_ratio, 4),
            "minimum": _serialise(self.minimum),
            "maximum": _serialise(self.maximum),
            "mean": None if self.mean is None else round(self.mean, 4),
            "top_values": [{"value": value, "count": count} for value, count in self.top_values],
            "outlier_count": self.outlier_count,
        }


@dataclass(frozen=True, slots=True)
class Finding:
    """Something worth a human's attention.

    `severity` separates "this is broken" from "this is worth knowing". A
    profile that flags everything at the same weight gets skimmed.
    """

    kind: str
    severity: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "severity": self.severity, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class TableProfile:
    """One table, measured."""

    table: str
    rows: int
    columns: int
    column_profiles: tuple[ColumnProfile, ...] = ()
    findings: tuple[Finding, ...] = ()
    duplicate_candidates: int = 0

    @property
    def quality_score(self) -> float:
        """A single number for "how clean is this table", 0-100.

        Deliberately crude and deliberately transparent: it is the findings
        weighted by severity, subtracted from 100. It exists to rank tables
        for attention, not to be an authority — the findings underneath are
        what a reader should act on, and they are always shown alongside it.
        """
        penalty = 0.0
        for finding in self.findings:
            penalty += {"error": 12.0, "warning": 4.0, "info": 1.0}.get(finding.severity, 1.0)
        return max(0.0, round(100.0 - penalty, 1))

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "rows": self.rows,
            "columns": self.columns,
            "quality_score": self.quality_score,
            "duplicate_candidates": self.duplicate_candidates,
            "column_profiles": [column.to_dict() for column in self.column_profiles],
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True, slots=True)
class WarehouseProfile:
    """Every table, plus what was found across them."""

    tables: tuple[TableProfile, ...] = field(default_factory=tuple)

    @property
    def total_rows(self) -> int:
        return sum(table.rows for table in self.tables)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tables": [table.to_dict() for table in self.tables],
            "total_rows": self.total_rows,
            "table_count": len(self.tables),
        }


def _serialise(value: Any) -> Any:
    """JSON-safe form of a database value."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _is_numeric(type_name: str) -> bool:
    lowered = type_name.lower()
    return any(term in lowered for term in ("int", "numeric", "decimal", "float", "double", "real"))


def _is_temporal(type_name: str) -> bool:
    lowered = type_name.lower()
    return "date" in lowered or "time" in lowered


def _is_orderable(type_name: str) -> bool:
    return _is_numeric(type_name) or _is_temporal(type_name)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _aggregate_sql(table: TableInfo) -> str:
    """One SELECT computing every per-column statistic in a single scan."""
    parts = ["count(*) AS __rows"]
    for column in table.columns:
        name = f'"{column.name}"'
        parts.append(f"count(*) FILTER (WHERE {name} IS NULL) AS {column.name}__nulls")
        parts.append(f"count(DISTINCT {name}) AS {column.name}__distinct")
        if _is_orderable(column.type_name):
            parts.append(f"min({name}) AS {column.name}__min")
            parts.append(f"max({name}) AS {column.name}__max")
        if _is_numeric(column.type_name):
            parts.append(f"avg({name}) AS {column.name}__mean")
            parts.append(
                f"percentile_cont(0.25) WITHIN GROUP (ORDER BY {name}) AS {column.name}__q1"
            )
            parts.append(
                f"percentile_cont(0.75) WITHIN GROUP (ORDER BY {name}) AS {column.name}__q3"
            )
    return f'SELECT {", ".join(parts)} FROM "{table.name}"'  # noqa: S608


def _top_values(table: str, column: str, limit: int) -> tuple[tuple[str, int], ...]:
    """The most common values in a low-cardinality column."""
    sql = (
        f'SELECT "{column}"::text AS value, count(*) AS n FROM "{table}" '  # noqa: S608
        f'WHERE "{column}" IS NOT NULL GROUP BY 1 ORDER BY n DESC, 1 LIMIT {limit}'
    )
    result = execute_readonly(sql, max_rows=limit)
    return tuple((str(row[0]), int(row[1])) for row in result.rows)


def _outlier_count(table: str, column: str, low: float, high: float) -> int:
    sql = (
        f'SELECT count(*) FROM "{table}" '  # noqa: S608
        f'WHERE "{column}" IS NOT NULL AND ("{column}" < {low} OR "{column}" > {high})'
    )
    result = execute_readonly(sql, max_rows=1)
    return int(result.rows[0][0]) if result.rows else 0


def _duplicate_candidates(table: TableInfo) -> int:
    """Rows identical on every non-key column.

    The primary key makes every row technically distinct, so duplicates are
    only meaningful once it is ignored. Two orders identical in customer,
    date, status and every amount are a data-entry artefact whatever their
    ids say.
    """
    comparable = [
        column.name
        for column in table.columns
        if not column.is_primary_key and column.name not in AUDIT_COLUMNS
    ]
    if len(comparable) < 2:
        return 0

    columns = ", ".join(f'"{name}"' for name in comparable)
    sql = (
        f'SELECT count(*) FROM (SELECT {columns} FROM "{table.name}" '  # noqa: S608
        f"GROUP BY {columns} HAVING count(*) > 1) AS duplicated"
    )
    result = execute_readonly(sql, max_rows=1)
    return int(result.rows[0][0]) if result.rows else 0


def _orphan_count(table: str, column: str, target: str) -> int:
    """Rows whose foreign key points at nothing.

    A declared constraint should make this impossible. Measuring it anyway is
    the point: the profile reports what the data does, not what the schema
    promises, and the two can diverge if a constraint was ever added NOT VALID
    or dropped.
    """
    target_table, _, target_column = target.partition(".")
    sql = (
        f'SELECT count(*) FROM "{table}" child '  # noqa: S608
        f'WHERE child."{column}" IS NOT NULL AND NOT EXISTS ('
        f'SELECT 1 FROM "{target_table}" parent '
        f'WHERE parent."{target_column}" = child."{column}")'
    )
    result = execute_readonly(sql, max_rows=1)
    return int(result.rows[0][0]) if result.rows else 0


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------


def _column_profile(
    column: ColumnInfo, values: dict[str, Any], rows: int, table_name: str
) -> ColumnProfile:
    nulls = int(values.get(f"{column.name}__nulls") or 0)
    distinct = int(values.get(f"{column.name}__distinct") or 0)
    non_null = rows - nulls

    minimum = values.get(f"{column.name}__min")
    maximum = values.get(f"{column.name}__max")
    mean_raw = values.get(f"{column.name}__mean")
    mean = float(mean_raw) if mean_raw is not None else None

    top: tuple[tuple[str, int], ...] = ()
    if 0 < distinct <= MAX_CATEGORICAL_VALUES and not column.is_primary_key:
        top = _top_values(table_name, column.name, MAX_CATEGORICAL_VALUES)

    outliers = 0
    q1 = values.get(f"{column.name}__q1")
    q3 = values.get(f"{column.name}__q3")
    if q1 is not None and q3 is not None and non_null > 0:
        spread = float(q3) - float(q1)
        if spread > 0:
            low = float(q1) - OUTLIER_IQR_MULTIPLIER * spread
            high = float(q3) + OUTLIER_IQR_MULTIPLIER * spread
            outliers = _outlier_count(table_name, column.name, low, high)

    return ColumnProfile(
        name=column.name,
        type_name=column.type_name,
        nullable=column.nullable,
        null_count=nulls,
        null_percent=(nulls / rows * 100.0) if rows else 0.0,
        distinct_count=distinct,
        cardinality_ratio=(distinct / non_null) if non_null else 0.0,
        minimum=minimum,
        maximum=maximum,
        mean=mean,
        top_values=top,
        outlier_count=outliers,
    )


def _findings(
    table: TableInfo,
    profiles: list[ColumnProfile],
    duplicates: int,
    total_rows: int,
) -> list[Finding]:
    """Turn measurements into things worth saying."""
    found: list[Finding] = []

    if duplicates:
        found.append(
            Finding(
                "duplicate_rows",
                "warning",
                f"{duplicates:,} group(s) of rows identical on every non-key column.",
            )
        )

    for column in table.columns:
        target = column.foreign_key
        if not target:
            continue
        orphans = _orphan_count(table.name, column.name, target)
        if orphans:
            found.append(
                Finding(
                    "orphaned_foreign_key",
                    "error",
                    f"{column.name}: {orphans:,} row(s) reference a missing {target}.",
                )
            )

    by_name = {profile.name: profile for profile in profiles}
    for column in table.columns:
        profile = by_name.get(column.name)
        if profile is None:
            continue

        if profile.null_percent >= 50.0:
            found.append(
                Finding(
                    "mostly_null",
                    "warning",
                    f"{profile.name} is {profile.null_percent:.1f}% null.",
                )
            )
        elif profile.null_percent > 0 and not column.nullable:
            # Should be impossible; if it happens the constraint is not real.
            found.append(
                Finding(
                    "null_in_non_nullable",
                    "error",
                    f"{profile.name} is declared NOT NULL but holds nulls.",
                )
            )

        if (
            profile.distinct_count == 1
            and not column.is_primary_key
            and column.name not in AUDIT_COLUMNS
        ):
            found.append(
                Finding(
                    "constant_column",
                    "info",
                    f"{profile.name} holds one value for every row, so it cannot discriminate.",
                )
            )

        share = profile.outlier_count / total_rows if total_rows else 0.0
        if profile.outlier_count and share <= MAX_OUTLIER_SHARE:
            found.append(
                Finding(
                    "numeric_outliers",
                    "info",
                    (
                        f"{profile.name}: {profile.outlier_count:,} value(s) "
                        f"({share * 100:.2f}%) beyond {OUTLIER_IQR_MULTIPLIER:g} IQRs "
                        "of the quartiles."
                    ),
                )
            )

    return found


def profile_table(table: TableInfo) -> TableProfile:
    """Measure one table."""
    result = execute_readonly(_aggregate_sql(table), max_rows=1)
    if not result.rows:
        return TableProfile(table=table.name, rows=0, columns=len(table.columns))

    values = dict(zip(result.columns, result.rows[0], strict=False))
    rows = int(values.get("__rows") or 0)

    profiles = [_column_profile(column, values, rows, table.name) for column in table.columns]
    duplicates = _duplicate_candidates(table) if rows else 0

    return TableProfile(
        table=table.name,
        rows=rows,
        columns=len(table.columns),
        column_profiles=tuple(profiles),
        findings=tuple(_findings(table, profiles, duplicates, rows)),
        duplicate_candidates=duplicates,
    )


def profile_warehouse(tables: list[str] | None = None) -> WarehouseProfile:
    """Measure every table, or the named subset."""
    schema = get_schema()
    names = [name for name in sorted(schema) if tables is None or name in tables]
    logger.info("profiling %s table(s)", len(names))
    return WarehouseProfile(tuple(profile_table(schema[name]) for name in names))
