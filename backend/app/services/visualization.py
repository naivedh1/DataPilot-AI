"""Chart selection and Plotly specification.

Chart choice is **deterministic, driven by the shape of the result**, not by the
language model. A model asked "what chart?" will happily suggest a pie chart for
forty categories or a line chart for unordered labels. The result's structure
already answers the question: a date column plus one measure is a line chart;
there is nothing to deliberate about.

Being deterministic also means chart selection is unit-testable and costs no
tokens and no latency.

The output is a specification, not an image. The frontend renders it with
Plotly, so the chart stays interactive — hoverable, zoomable, inspectable — and
the underlying figures remain visible rather than baked into a picture.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from app.database.executor import QueryResult
from app.services.measures import humanize as _humanize
from app.services.measures import is_additive
from app.services.measures import value_format as _value_format

logger = logging.getLogger(__name__)

#: Above this many categories a bar chart becomes unreadable; show a table.
MAX_BAR_CATEGORIES = 30

#: A pie chart is only honest for a handful of slices that sum to a whole.
MAX_PIE_SLICES = 6

#: Below this many points a line chart implies a trend that is not there.
MIN_LINE_POINTS = 3


@dataclass(frozen=True, slots=True)
class ChartSeries:
    """One plotted series."""

    name: str
    values: list[Any]


@dataclass(frozen=True, slots=True)
class ChartPayload:
    """A renderable chart specification.

    Deliberately framework-neutral: it names a chart type, axes and data. The
    frontend maps it onto Plotly traces, so the backend never depends on a
    plotting library's object model.
    """

    chart_type: str
    title: str
    x_label: str = ""
    y_label: str = ""
    x_values: list[Any] = field(default_factory=list)
    series: list[ChartSeries] = field(default_factory=list)
    #: Why this form was chosen. Shown in the UI so the choice is inspectable.
    rationale: str = ""
    #: Hint for the frontend's number formatting.
    value_format: str = "number"

    def to_dict(self) -> dict[str, Any]:
        return {
            "chart_type": self.chart_type,
            "title": self.title,
            "x_label": self.x_label,
            "y_label": self.y_label,
            "x_values": self.x_values,
            "series": [{"name": series.name, "values": series.values} for series in self.series],
            "rationale": self.rationale,
            "value_format": self.value_format,
        }


def _is_temporal(series: pd.Series) -> bool:
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    return bool(series.map(lambda v: isinstance(v, date | datetime)).any())


def _is_numeric(series: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _frame(result: QueryResult) -> pd.DataFrame:
    """Result rows as a DataFrame, with Decimal widened to float.

    Duplicated rather than imported from `analysis` so the two modules stay
    independent; it is four lines, and a shared import would reintroduce the
    cycle this module was split to avoid.
    """
    frame = pd.DataFrame(result.rows, columns=result.columns)
    for column in frame.columns:
        if frame[column].map(lambda v: isinstance(v, Decimal)).any():
            frame[column] = frame[column].astype(float)
    return frame


def select_chart(result: QueryResult, question: str = "") -> ChartPayload:
    """Choose and build a chart for a result set.

    Falls back to `table` whenever a chart would not genuinely help. A chart
    that adds nothing is worse than no chart: it implies a pattern the data does
    not contain.
    """
    if result.is_empty:
        return ChartPayload(
            chart_type="table",
            title="No results",
            rationale="The query returned no rows.",
        )

    frame = _frame(result)
    numeric = [column for column in frame.columns if _is_numeric(frame[column])]
    temporal = [column for column in frame.columns if _is_temporal(frame[column])]
    categorical = [
        column for column in frame.columns if column not in numeric and column not in temporal
    ]

    title = question.strip().rstrip("?") if question else "Query result"

    # A single value is a number, not a chart.
    if result.row_count == 1 and len(numeric) == 1 and not categorical:
        return ChartPayload(
            chart_type="table",
            title=title,
            rationale="A single value is better read as a number than plotted.",
            value_format=_value_format(numeric[0]),
        )

    if not numeric:
        return ChartPayload(
            chart_type="table",
            title=title,
            rationale="No numeric column to plot.",
        )

    measure = numeric[-1]

    # Time series -> line.
    if temporal and result.row_count >= MIN_LINE_POINTS:
        return _build_time_series(frame, temporal[0], numeric, categorical, title)

    # Category + measure -> bar.
    if categorical:
        return _build_categorical(frame, categorical, measure, title, result.row_count)

    # Two measures, many points -> scatter.
    if len(numeric) >= 2 and result.row_count >= MIN_LINE_POINTS:
        return _build_scatter(frame, numeric[0], numeric[1], title)

    # One measure, many rows, no labels -> distribution.
    if len(numeric) == 1 and result.row_count >= 10:
        return ChartPayload(
            chart_type="histogram",
            title=title,
            x_label=_humanize(measure),
            y_label="Frequency",
            x_values=[_jsonable(value) for value in frame[measure]],
            series=[ChartSeries(name=_humanize(measure), values=[])],
            rationale="A single measure across many rows is best shown as a distribution.",
            value_format=_value_format(measure),
        )

    return ChartPayload(
        chart_type="table",
        title=title,
        rationale="The result's shape does not suit any chart form.",
    )


def _build_time_series(
    frame: pd.DataFrame,
    time_column: str,
    numeric: list[str],
    categorical: list[str],
    title: str,
) -> ChartPayload:
    ordered = frame.sort_values(time_column)
    measure = numeric[-1]

    # A category column alongside time means one line per category.
    if categorical:
        split = categorical[0]
        distinct = ordered[split].nunique()
        if 1 < distinct <= 12:
            periods = sorted(ordered[time_column].unique())
            series: list[ChartSeries] = []
            for name, group in ordered.groupby(split, sort=True):
                lookup = dict(zip(group[time_column], group[measure], strict=False))
                series.append(
                    ChartSeries(
                        name=str(name),
                        values=[_jsonable(lookup.get(period)) for period in periods],
                    )
                )
            return ChartPayload(
                chart_type="line",
                title=title,
                x_label=_humanize(time_column),
                y_label=_humanize(measure),
                x_values=[_jsonable(period) for period in periods],
                series=series,
                rationale=(
                    f"A measure over time split by {_humanize(split).lower()} — "
                    "one line per group makes the comparison direct."
                ),
                value_format=_value_format(measure),
            )

    plotted = numeric if len(numeric) <= 3 else [measure]
    return ChartPayload(
        chart_type="line",
        title=title,
        x_label=_humanize(time_column),
        y_label=_humanize(plotted[0]),
        x_values=[_jsonable(value) for value in ordered[time_column]],
        series=[
            ChartSeries(
                name=_humanize(column),
                values=[_jsonable(value) for value in ordered[column]],
            )
            for column in plotted
        ],
        rationale="A measure over time reads most clearly as a line.",
        value_format=_value_format(plotted[0]),
    )


def _build_categorical(
    frame: pd.DataFrame,
    categorical: list[str],
    measure: str,
    title: str,
    row_count: int,
) -> ChartPayload:
    label_column = categorical[0]

    if row_count > MAX_BAR_CATEGORIES:
        return ChartPayload(
            chart_type="table",
            title=title,
            rationale=(
                f"{row_count} categories is too many to read as a bar chart; a table is clearer."
            ),
            value_format=_value_format(measure),
        )

    # Two categorical columns -> grouped bars, if the second is small enough.
    if len(categorical) >= 2:
        split = categorical[1]
        distinct = frame[split].nunique()
        if 1 < distinct <= 6:
            labels = list(dict.fromkeys(frame[label_column]))
            series = []
            for name, group in frame.groupby(split, sort=True):
                lookup = dict(zip(group[label_column], group[measure], strict=False))
                series.append(
                    ChartSeries(
                        name=str(name),
                        values=[_jsonable(lookup.get(label)) for label in labels],
                    )
                )
            return ChartPayload(
                chart_type="grouped_bar",
                title=title,
                x_label=_humanize(label_column),
                y_label=_humanize(measure),
                x_values=[_jsonable(label) for label in labels],
                series=series,
                rationale=(
                    f"Two dimensions ({_humanize(label_column).lower()} and "
                    f"{_humanize(split).lower()}) compare best as grouped bars."
                ),
                value_format=_value_format(measure),
            )

    ordered = frame.sort_values(measure, ascending=False)
    value_format = _value_format(measure)

    # A pie is only honest for a few slices of a genuine whole. It requires an
    # *additive* measure: a pie of average order value by segment would assert
    # that three averages compose a total, which is not a real quantity.
    if row_count <= MAX_PIE_SLICES and is_additive(measure):
        values = ordered[measure]
        if (values >= 0).all() and float(values.sum()) > 0:
            return ChartPayload(
                chart_type="pie",
                title=title,
                x_label=_humanize(label_column),
                y_label=_humanize(measure),
                x_values=[_jsonable(value) for value in ordered[label_column]],
                series=[
                    ChartSeries(
                        name=_humanize(measure),
                        values=[_jsonable(value) for value in values],
                    )
                ],
                rationale=(
                    f"{row_count} non-negative parts of a total — a pie shows composition directly."
                ),
                value_format=value_format,
            )

    return ChartPayload(
        chart_type="bar",
        title=title,
        x_label=_humanize(label_column),
        y_label=_humanize(measure),
        x_values=[_jsonable(value) for value in ordered[label_column]],
        series=[
            ChartSeries(
                name=_humanize(measure),
                values=[_jsonable(value) for value in ordered[measure]],
            )
        ],
        rationale="Comparing a measure across categories reads best as bars.",
        value_format=value_format,
    )


def _build_scatter(frame: pd.DataFrame, x_column: str, y_column: str, title: str) -> ChartPayload:
    return ChartPayload(
        chart_type="scatter",
        title=title,
        x_label=_humanize(x_column),
        y_label=_humanize(y_column),
        x_values=[_jsonable(value) for value in frame[x_column]],
        series=[
            ChartSeries(
                name=_humanize(y_column),
                values=[_jsonable(value) for value in frame[y_column]],
            )
        ],
        rationale="Two numeric measures show their relationship as a scatter plot.",
        value_format=_value_format(y_column),
    )
