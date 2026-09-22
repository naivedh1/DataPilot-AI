"""Tests for deterministic data profiling.

The finding logic is tested on synthetic profiles rather than against the
warehouse. That is the only way to cover the cases that matter — a NOT NULL
column holding nulls, a mostly-empty column, a zero-inflated numeric column
whose "outliers" are really its distribution. A clean warehouse produces none
of them on demand, which is exactly why it cannot be the test fixture.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.profiler import (
    MAX_OUTLIER_SHARE,
    ColumnProfile,
    Finding,
    TableProfile,
    _aggregate_sql,
    _findings,
    _is_numeric,
    _is_temporal,
    _serialise,
)
from app.services.schema.introspect import ColumnInfo, TableInfo


def _column(
    name: str,
    type_name: str = "INTEGER",
    *,
    nullable: bool = True,
    primary_key: bool = False,
    foreign_key: str | None = None,
) -> ColumnInfo:
    return ColumnInfo(
        name=name,
        type_name=type_name,
        nullable=nullable,
        is_primary_key=primary_key,
        description="",
        foreign_key=foreign_key,
    )


def _table(*columns: ColumnInfo, name: str = "widgets") -> TableInfo:
    return TableInfo(name=name, description="", columns=tuple(columns))


def _profile(name: str, **overrides: object) -> ColumnProfile:
    base: dict[str, object] = {
        "name": name,
        "type_name": "INTEGER",
        "nullable": True,
        "null_count": 0,
        "null_percent": 0.0,
        "distinct_count": 50,
        "cardinality_ratio": 0.5,
    }
    base.update(overrides)
    return ColumnProfile(**base)  # type: ignore[arg-type]


def _kinds(findings: list[Finding]) -> set[str]:
    return {finding.kind for finding in findings}


class TestTypeDetection:
    def test_recognises_numeric_types(self):
        for name in ("INTEGER", "NUMERIC(12, 2)", "DOUBLE PRECISION", "BIGINT", "REAL"):
            assert _is_numeric(name), name

    def test_does_not_treat_text_as_numeric(self):
        for name in ("VARCHAR(32)", "TEXT", "BOOLEAN"):
            assert not _is_numeric(name), name

    def test_recognises_temporal_types(self):
        for name in ("DATE", "TIMESTAMP WITHOUT TIME ZONE"):
            assert _is_temporal(name), name


class TestAggregateQuery:
    def test_reads_the_table_once(self):
        """One SELECT per table, not per column: twelve columns profiled
        separately would be twelve sequential scans."""
        sql = _aggregate_sql(_table(_column("a"), _column("b", "VARCHAR(8)")))
        assert sql.upper().count("FROM") == 1
        assert sql.lstrip().upper().startswith("SELECT")

    def test_asks_for_quartiles_only_on_numeric_columns(self):
        sql = _aggregate_sql(_table(_column("amount", "NUMERIC(12, 2)"), _column("label", "TEXT")))
        assert "amount__q1" in sql and "amount__q3" in sql
        assert "label__q1" not in sql

    def test_quotes_identifiers(self):
        """An unquoted column named `order` or `user` would not parse."""
        sql = _aggregate_sql(_table(_column("order"), name="user"))
        assert '"order"' in sql and '"user"' in sql


class TestFindings:
    def test_a_clean_table_produces_nothing(self):
        table = _table(_column("id", primary_key=True, nullable=False), _column("amount"))
        findings = _findings(table, [_profile("id"), _profile("amount")], 0, 100)
        assert findings == []

    def test_duplicates_are_reported(self):
        findings = _findings(_table(_column("a"), _column("b")), [], 7, 100)
        assert "duplicate_rows" in _kinds(findings)

    def test_a_not_null_column_holding_nulls_is_an_error(self):
        """Should be impossible. If it happens, the constraint is not real —
        which is a stronger statement than any warning."""
        table = _table(_column("code", nullable=False))
        findings = _findings(table, [_profile("code", null_count=3, null_percent=3.0)], 0, 100)
        assert "null_in_non_nullable" in _kinds(findings)
        assert all(f.severity == "error" for f in findings if f.kind == "null_in_non_nullable")

    def test_a_mostly_null_column_is_a_warning(self):
        table = _table(_column("note"))
        findings = _findings(table, [_profile("note", null_count=80, null_percent=80.0)], 0, 100)
        assert "mostly_null" in _kinds(findings)

    def test_a_constant_column_is_reported(self):
        table = _table(_column("region"))
        findings = _findings(table, [_profile("region", distinct_count=1)], 0, 100)
        assert "constant_column" in _kinds(findings)

    def test_a_constant_primary_key_is_not_reported(self):
        table = _table(_column("id", primary_key=True))
        findings = _findings(table, [_profile("id", distinct_count=1)], 0, 100)
        assert "constant_column" not in _kinds(findings)

    def test_audit_columns_are_not_reported_as_constant(self):
        """`created_at` is written by the database on insert, so a bulk-loaded
        warehouse has one value for every row. That is a fact about the load,
        not about the data."""
        table = _table(_column("created_at", "TIMESTAMP"))
        findings = _findings(table, [_profile("created_at", distinct_count=1)], 0, 100)
        assert "constant_column" not in _kinds(findings)

    def test_a_few_outliers_are_reported(self):
        table = _table(_column("amount", "NUMERIC(12, 2)"))
        findings = _findings(table, [_profile("amount", outlier_count=2)], 0, 1000)
        assert "numeric_outliers" in _kinds(findings)

    def test_a_zero_inflated_column_is_not_reported_as_outliers(self):
        """With Q1 = 0 and a tiny IQR, every non-zero row falls outside the
        fence. Flagging 22% of a table as anomalous teaches the reader to
        ignore the finding."""
        table = _table(_column("discount_amount", "NUMERIC(12, 2)"))
        share = MAX_OUTLIER_SHARE * 4
        findings = _findings(
            table, [_profile("discount_amount", outlier_count=int(1000 * share))], 0, 1000
        )
        assert "numeric_outliers" not in _kinds(findings)


class TestQualityScore:
    def test_a_clean_table_scores_one_hundred(self):
        assert TableProfile("t", 10, 2).quality_score == 100.0

    def test_errors_cost_more_than_warnings_which_cost_more_than_info(self):
        def score(severity: str) -> float:
            return TableProfile("t", 10, 2, findings=(Finding("k", severity, ""),)).quality_score

        assert score("error") < score("warning") < score("info") < 100.0

    def test_the_score_never_goes_negative(self):
        many = tuple(Finding(f"k{i}", "error", "") for i in range(50))
        assert TableProfile("t", 10, 2, findings=many).quality_score == 0.0


class TestSerialisation:
    def test_decimals_become_floats(self):
        assert _serialise(Decimal("12.34")) == 12.34

    def test_dates_become_iso_strings(self):
        assert _serialise(dt.date(2026, 2, 1)) == "2026-02-01"

    def test_plain_values_pass_through(self):
        assert _serialise(7) == 7
        assert _serialise(None) is None

    def test_column_profile_round_trips_to_json_safe_types(self):
        payload = _profile(
            "amount",
            minimum=Decimal("1.5"),
            maximum=Decimal("99.5"),
            mean=50.0,
            top_values=(("a", 3),),
        ).to_dict()
        assert payload["minimum"] == 1.5
        assert payload["top_values"] == [{"value": "a", "count": 3}]
