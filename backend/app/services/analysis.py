"""Pandas analysis of query results.

Everything here is **computed, never generated**. That is the point: the insight
node is given these figures and told to use them, so the numbers in the final
answer come from arithmetic rather than from a language model's impression of
the data. Groundedness is enforced by construction.

The division of labour with SQL is deliberate. Aggregation belongs in the
database — it is faster, and it works on rows the row cap never returned.
Pandas handles what comes *after* aggregation: period-over-period change,
concentration, outlier detection. Recomputing a SUM here that SQL already
computed would be slower and, under a row cap, wrong.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from app.database.executor import QueryResult
from app.services.measures import is_additive

logger = logging.getLogger(__name__)

#: Below this many periods, a trend is noise rather than a trend.
MIN_PERIODS_FOR_TREND = 3

#: Z-score past which a point is flagged. 2.5 keeps the flag rate low enough
#: that a flag means something.
OUTLIER_Z_THRESHOLD = 2.5

#: Minimum points before outlier detection is meaningful. With fewer, a single
#: value dominates the standard deviation and everything looks normal.
MIN_POINTS_FOR_OUTLIERS = 6


@dataclass(frozen=True, slots=True)
class TrendAnalysis:
    """Change in a measure across an ordered series."""

    column: str
    first_value: float
    last_value: float
    change: float
    percent_change: float | None
    direction: str
    periods: int
    largest_increase: tuple[str, float] | None = None
    largest_decrease: tuple[str, float] | None = None


@dataclass(frozen=True, slots=True)
class RankingAnalysis:
    """Concentration within a ranked categorical breakdown."""

    label_column: str
    value_column: str
    top_label: str
    top_value: float
    total: float
    top_share: float
    top_three_share: float
    categories: int


@dataclass(frozen=True, slots=True)
class OutlierPoint:
    """A value far enough from the mean to be worth a second look."""

    label: str
    value: float
    z_score: float
    direction: str


@dataclass(frozen=True, slots=True)
class ColumnStats:
    """Descriptive statistics for one numeric column."""

    column: str
    count: int
    total: float
    mean: float
    median: float
    minimum: float
    maximum: float
    std_dev: float


@dataclass(slots=True)
class AnalysisReport:
    """Everything computed from one result set."""

    row_count: int
    numeric_columns: list[str] = field(default_factory=list)
    categorical_columns: list[str] = field(default_factory=list)
    temporal_columns: list[str] = field(default_factory=list)
    stats: list[ColumnStats] = field(default_factory=list)
    trend: TrendAnalysis | None = None
    ranking: RankingAnalysis | None = None
    outliers: list[OutlierPoint] = field(default_factory=list)
    correlation: tuple[str, str, float] | None = None

    def summarize(self) -> str:
        """A compact text summary for the insight prompt.

        These figures are labelled reliable in the prompt because they are
        computed from the full result set, so the model may cite them directly.
        """
        lines: list[str] = [f"Rows returned: {self.row_count}"]

        for stat in self.stats:
            # A "total" of averages or rates is not a quantity; omitting it
            # keeps a fabricated figure out of the insight prompt entirely.
            total = f"total={stat.total:,.2f} " if is_additive(stat.column) else ""
            lines.append(
                f"{stat.column}: {total}mean={stat.mean:,.2f} "
                f"median={stat.median:,.2f} min={stat.minimum:,.2f} "
                f"max={stat.maximum:,.2f}"
            )

        if self.trend:
            trend = self.trend
            change = (
                f"{trend.percent_change:+.1f}%"
                if trend.percent_change is not None
                else f"{trend.change:+,.2f}"
            )
            lines.append(
                f"Trend in {trend.column} across {trend.periods} periods: "
                f"{trend.first_value:,.2f} -> {trend.last_value:,.2f} ({change}, "
                f"{trend.direction})"
            )
            if trend.largest_increase:
                label, value = trend.largest_increase
                lines.append(f"Largest period-on-period rise: {label} ({value:+,.2f})")
            if trend.largest_decrease:
                label, value = trend.largest_decrease
                lines.append(f"Largest period-on-period fall: {label} ({value:+,.2f})")

        if self.ranking:
            rank = self.ranking
            if rank.total > 0:
                lines.append(
                    f"Top {rank.label_column}: {rank.top_label} at "
                    f"{rank.top_value:,.2f} ({rank.top_share:.1f}% of the "
                    f"{rank.total:,.2f} total across {rank.categories} categories); "
                    f"top three hold {rank.top_three_share:.1f}%"
                )
            else:
                # Non-additive measure: report the leader, but no share of a
                # total that does not exist.
                lines.append(
                    f"Highest {rank.label_column}: {rank.top_label} at "
                    f"{rank.top_value:,.2f} (of {rank.categories} categories). "
                    f"{rank.value_column} is not additive, so no share-of-total "
                    "is reported."
                )

        for outlier in self.outliers:
            lines.append(
                f"Outlier: {outlier.label} at {outlier.value:,.2f} "
                f"({outlier.z_score:+.1f} standard deviations, {outlier.direction})"
            )

        if self.correlation:
            left, right, value = self.correlation
            lines.append(
                f"Correlation between {left} and {right}: {value:+.2f} "
                "(association only, not causation)"
            )

        return "\n".join(lines)


def to_dataframe(result: QueryResult) -> pd.DataFrame:
    """Build a DataFrame from a query result.

    Decimal is converted to float *here and only here*. Money is kept exact
    through the database and the executor; float is acceptable for statistics
    but would be wrong for storage or for the financial identities.
    """
    frame = pd.DataFrame(result.rows, columns=result.columns)
    for column in frame.columns:
        if frame[column].map(lambda v: isinstance(v, Decimal)).any():
            frame[column] = frame[column].astype(float)
    return frame


def _classify_columns(frame: pd.DataFrame) -> tuple[list[str], list[str], list[str]]:
    """Split columns into numeric, categorical and temporal."""
    numeric: list[str] = []
    categorical: list[str] = []
    temporal: list[str] = []

    for column in frame.columns:
        series = frame[column]
        if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
            numeric.append(column)
        elif (
            pd.api.types.is_datetime64_any_dtype(series)
            or series.map(lambda v: isinstance(v, date | datetime)).any()
        ):
            temporal.append(column)
        else:
            categorical.append(column)

    return numeric, categorical, temporal


def _column_stats(frame: pd.DataFrame, column: str) -> ColumnStats | None:
    series = frame[column].dropna()
    if series.empty:
        return None
    return ColumnStats(
        column=column,
        count=int(series.count()),
        total=float(series.sum()),
        mean=float(series.mean()),
        median=float(series.median()),
        minimum=float(series.min()),
        maximum=float(series.max()),
        std_dev=float(series.std()) if len(series) > 1 else 0.0,
    )


def _analyse_trend(
    frame: pd.DataFrame, time_column: str, value_column: str
) -> TrendAnalysis | None:
    ordered = frame[[time_column, value_column]].dropna().sort_values(time_column)
    if len(ordered) < MIN_PERIODS_FOR_TREND:
        return None

    values = ordered[value_column].astype(float)
    first, last = float(values.iloc[0]), float(values.iloc[-1])
    change = last - first
    # Percent change is undefined against a zero base — reporting "infinite
    # growth" would be worse than reporting nothing.
    percent = (change / abs(first) * 100) if first != 0 else None

    if percent is not None:
        direction = "rising" if percent > 1 else "falling" if percent < -1 else "flat"
    else:
        direction = "rising" if change > 0 else "falling" if change < 0 else "flat"

    deltas = values.diff()
    largest_increase = largest_decrease = None
    if len(deltas.dropna()):
        rise_index = deltas.idxmax()
        fall_index = deltas.idxmin()
        if float(deltas[rise_index]) > 0:
            largest_increase = (
                str(ordered.loc[rise_index, time_column]),
                float(deltas[rise_index]),
            )
        if float(deltas[fall_index]) < 0:
            largest_decrease = (
                str(ordered.loc[fall_index, time_column]),
                float(deltas[fall_index]),
            )

    return TrendAnalysis(
        column=value_column,
        first_value=first,
        last_value=last,
        change=change,
        percent_change=percent,
        direction=direction,
        periods=len(ordered),
        largest_increase=largest_increase,
        largest_decrease=largest_decrease,
    )


def _analyse_ranking(
    frame: pd.DataFrame, label_column: str, value_column: str
) -> RankingAnalysis | None:
    data = frame[[label_column, value_column]].dropna()
    if data.empty:
        return None

    ordered = data.sort_values(value_column, ascending=False)
    values = ordered[value_column].astype(float)

    # Share-of-total only means something for an additive measure. The sum of
    # three segments' average order values is not a total anyone would quote,
    # so reporting "Enterprise is 88% of it" would be a fabricated statistic.
    if not is_additive(value_column):
        return RankingAnalysis(
            label_column=label_column,
            value_column=value_column,
            top_label=str(ordered.iloc[0][label_column]),
            top_value=float(values.iloc[0]),
            total=0.0,
            top_share=0.0,
            top_three_share=0.0,
            categories=len(ordered),
        )

    total = float(values.sum())
    if total <= 0:
        return None

    return RankingAnalysis(
        label_column=label_column,
        value_column=value_column,
        top_label=str(ordered.iloc[0][label_column]),
        top_value=float(values.iloc[0]),
        total=total,
        top_share=float(values.iloc[0]) / total * 100,
        top_three_share=float(values.head(3).sum()) / total * 100,
        categories=len(ordered),
    )


def _find_outliers(frame: pd.DataFrame, label_column: str, value_column: str) -> list[OutlierPoint]:
    """Flag points more than `OUTLIER_Z_THRESHOLD` standard deviations out.

    Requires a minimum number of points: with fewer, one extreme value inflates
    the standard deviation enough to hide itself.
    """
    data = frame[[label_column, value_column]].dropna()
    if len(data) < MIN_POINTS_FOR_OUTLIERS:
        return []

    values = data[value_column].astype(float)
    std = float(values.std())
    if std == 0 or np.isnan(std):
        return []

    mean = float(values.mean())
    scores = (values - mean) / std

    outliers = [
        OutlierPoint(
            label=str(data.iloc[position][label_column]),
            value=float(values.iloc[position]),
            z_score=float(score),
            direction="unusually high" if score > 0 else "unusually low",
        )
        for position, score in enumerate(scores)
        if abs(score) >= OUTLIER_Z_THRESHOLD
    ]
    return sorted(outliers, key=lambda point: abs(point.z_score), reverse=True)[:3]


def analyse(result: QueryResult) -> AnalysisReport:
    """Compute everything derivable from a result set.

    Which analyses run is decided by the *shape* of the result, not by the
    question. A time column plus a measure gives a trend; a category plus a
    measure gives a ranking. Running an inapplicable analysis would produce a
    number with no meaning, which is exactly what the insight node must not be
    handed.
    """
    if result.is_empty:
        return AnalysisReport(row_count=0)

    frame = to_dataframe(result)
    numeric, categorical, temporal = _classify_columns(frame)

    report = AnalysisReport(
        row_count=result.row_count,
        numeric_columns=numeric,
        categorical_columns=categorical,
        temporal_columns=temporal,
        stats=[stat for stat in (_column_stats(frame, column) for column in numeric) if stat],
    )

    if not numeric:
        return report

    # The last numeric column is conventionally the measure: SQL puts
    # dimensions first and aggregates last.
    measure = numeric[-1]

    if temporal and result.row_count >= MIN_PERIODS_FOR_TREND:
        report.trend = _analyse_trend(frame, temporal[0], measure)
        report.outliers = _find_outliers(frame, temporal[0], measure)
    elif categorical:
        report.ranking = _analyse_ranking(frame, categorical[0], measure)
        report.outliers = _find_outliers(frame, categorical[0], measure)

    if len(numeric) >= 2 and result.row_count >= MIN_POINTS_FOR_OUTLIERS:
        left, right = numeric[0], numeric[1]
        value = float(frame[left].corr(frame[right]))
        if not np.isnan(value) and abs(value) >= 0.5:
            report.correlation = (left, right, value)

    return report


def to_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame rows as JSON-serialisable dicts."""
    return [
        # Column labels are Hashable to pandas' stubs; they are always strings
        # here because every frame is built from SQL result column names.
        {str(key): _jsonable(value) for key, value in record.items()}
        for record in frame.to_dict(orient="records")
    ]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal | np.integer | np.floating):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if pd.isna(value):
        return None
    return value
