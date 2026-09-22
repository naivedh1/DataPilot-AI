"""Tests for deterministic result validation.

These run on hand-built result sets rather than against the warehouse: the
arithmetic under test is about the rows, not about where they came from, and
constructing the failing cases directly is the only way to cover them. The
reconciliation check is exercised against the live database in the integration
suite, where a real total query can be derived and run.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.validation import (
    CheckStatus,
    ValidationCheck,
    ValidationReport,
    build_total_query,
    check_group_reconciliation,
    check_measures_not_all_null,
    check_non_negative_measures,
    check_not_truncated,
    check_percentages_within_bounds,
    check_result_not_empty,
    validate_result,
)


def _check(report: ValidationReport, name: str) -> ValidationCheck:
    return next(check for check in report.checks if check.name == name)


class TestReportStatus:
    def test_a_report_with_only_skips_is_not_a_pass(self):
        """Reporting "passed" for checks that never ran would be the whole
        failure mode this module exists to avoid."""
        report = ValidationReport((ValidationCheck("x", CheckStatus.SKIPPED),))
        assert report.status == "not_verified"

    def test_any_failure_fails_the_report(self):
        report = ValidationReport(
            (
                ValidationCheck("a", CheckStatus.PASSED),
                ValidationCheck("b", CheckStatus.FAILED),
            )
        )
        assert report.status == "failed"

    def test_passes_when_something_passed_and_nothing_failed(self):
        report = ValidationReport(
            (
                ValidationCheck("a", CheckStatus.PASSED),
                ValidationCheck("b", CheckStatus.SKIPPED),
            )
        )
        assert report.status == "passed"


class TestEmptyResult:
    def test_no_rows_fails(self):
        assert check_result_not_empty(0).status is CheckStatus.FAILED

    def test_rows_pass(self):
        assert check_result_not_empty(5).status is CheckStatus.PASSED


class TestNonNegativeMeasures:
    def test_negative_revenue_fails(self):
        check = check_non_negative_measures(["region", "revenue"], [("North", Decimal("-10.00"))])
        assert check.status is CheckStatus.FAILED
        assert "revenue" in check.detail

    def test_positive_revenue_passes(self):
        check = check_non_negative_measures(["region", "revenue"], [("North", Decimal("10.00"))])
        assert check.status is CheckStatus.PASSED

    def test_negative_margin_is_allowed(self):
        """A margin, a change and a growth rate may all legitimately be
        negative; only counts and money may not."""
        check = check_non_negative_measures(["category", "margin_pct"], [("Apparel", -4.2)])
        assert check.status is not CheckStatus.FAILED

    def test_skipped_when_no_measure_column(self):
        check = check_non_negative_measures(["region"], [("North",)])
        assert check.status is CheckStatus.SKIPPED


class TestPercentageBounds:
    def test_rate_above_one_hundred_fails(self):
        check = check_percentages_within_bounds(["refund_rate_pct"], [(4300.0,)])
        assert check.status is CheckStatus.FAILED

    def test_rate_within_bounds_passes(self):
        check = check_percentages_within_bounds(["refund_rate_pct"], [(5.23,)])
        assert check.status is CheckStatus.PASSED

    def test_fraction_form_also_passes(self):
        check = check_percentages_within_bounds(["refund_rate"], [(0.0523,)])
        assert check.status is CheckStatus.PASSED

    def test_skipped_without_a_percentage_column(self):
        check = check_percentages_within_bounds(["revenue"], [(1.0,)])
        assert check.status is CheckStatus.SKIPPED


class TestAllNullMeasures:
    def test_entirely_null_column_fails(self):
        """The query ran, the shape is right, every number is missing — which
        a narrative layer will happily render as though it meant zero."""
        check = check_measures_not_all_null(["region", "revenue"], [("North", None), ("S", None)])
        assert check.status is CheckStatus.FAILED
        assert "revenue" in check.detail

    def test_partially_null_column_passes(self):
        check = check_measures_not_all_null(
            ["region", "revenue"], [("North", None), ("South", Decimal("5"))]
        )
        assert check.status is CheckStatus.PASSED


class TestTruncation:
    def test_truncated_result_fails(self):
        """Any total computed from a capped result is wrong, and the number
        does not show it."""
        assert check_not_truncated(True, 5000).status is CheckStatus.FAILED

    def test_untruncated_passes(self):
        assert check_not_truncated(False, 12).status is CheckStatus.PASSED


class TestBuildTotalQuery:
    def test_strips_grouping_and_keeps_the_aggregate(self):
        sql = build_total_query(
            "SELECT r.name AS region, SUM(o.total_amount) AS revenue "
            "FROM orders o JOIN regions r ON r.id = o.shipping_region_id "
            "GROUP BY r.name ORDER BY revenue DESC"
        )
        assert sql is not None
        assert "GROUP BY" not in sql.upper()
        assert "SUM(" in sql.upper()
        assert "r.name" not in sql

    def test_declines_a_query_with_no_grouping(self):
        assert build_total_query("SELECT SUM(total_amount) FROM orders") is None

    def test_declines_a_top_n_query(self):
        """A top-N answer is deliberately partial. Its rows are not meant to
        sum to the whole, so reconciling it would report the query working as
        intended as a failure."""
        sql = (
            "SELECT p.name, SUM(oi.line_total) AS revenue FROM order_items oi "
            "JOIN products p ON p.id = oi.product_id GROUP BY p.name "
            "ORDER BY revenue DESC LIMIT 5"
        )
        assert build_total_query(sql) is None

    def test_declines_a_cte(self):
        sql = "WITH x AS (SELECT 1 AS a) SELECT a, SUM(a) AS total FROM x GROUP BY a"
        assert build_total_query(sql) is None

    def test_declines_when_there_is_no_aggregate(self):
        assert build_total_query("SELECT status FROM orders GROUP BY status") is None

    def test_declines_unparseable_sql(self):
        assert build_total_query("SELECT FROM WHERE") is None

    def test_drops_having_so_omitted_groups_surface(self):
        """Keeping HAVING would make both sides exclude the same groups and
        the check could never fail."""
        sql = build_total_query(
            "SELECT category, SUM(x) AS revenue FROM t GROUP BY category HAVING SUM(x) > 100"
        )
        assert sql is not None
        assert "HAVING" not in sql.upper()


class TestReconciliation:
    def test_matching_parts_pass(self):
        check = check_group_reconciliation(
            ["region", "revenue"],
            [("N", Decimal("60")), ("S", Decimal("40"))],
            ["revenue"],
            [(Decimal("100"),)],
        )
        assert check.status is CheckStatus.PASSED

    def test_mismatched_parts_fail(self):
        check = check_group_reconciliation(
            ["region", "revenue"],
            [("N", Decimal("60")), ("S", Decimal("30"))],
            ["revenue"],
            [(Decimal("100"),)],
        )
        assert check.status is CheckStatus.FAILED
        assert "90" in check.detail and "100" in check.detail

    def test_non_additive_measures_are_not_reconciled(self):
        """The average of group averages is not the overall average, so
        comparing them would report arithmetic as a defect."""
        check = check_group_reconciliation(
            ["region", "average_order_value"],
            [("N", 100.0), ("S", 200.0)],
            ["average_order_value"],
            [(150.0,)],
        )
        assert check.status is CheckStatus.SKIPPED

    def test_skipped_when_no_total_rows(self):
        check = check_group_reconciliation(["region", "revenue"], [("N", 1.0)], ["revenue"], [])
        assert check.status is CheckStatus.SKIPPED


class TestValidateResult:
    def test_reconciliation_is_skipped_not_passed_without_a_total(self):
        report = validate_result(
            columns=["region", "revenue"],
            rows=[("N", Decimal("10"))],
            row_count=1,
            truncated=False,
        )
        assert _check(report, "group_reconciliation").status is CheckStatus.SKIPPED

    def test_a_clean_result_passes_overall(self):
        report = validate_result(
            columns=["region", "revenue"],
            rows=[("N", Decimal("60")), ("S", Decimal("40"))],
            row_count=2,
            truncated=False,
            total_columns=["revenue"],
            total_rows=[(Decimal("100"),)],
        )
        assert report.status == "passed"
        assert _check(report, "group_reconciliation").status is CheckStatus.PASSED

    def test_a_broken_result_fails_overall(self):
        report = validate_result(
            columns=["region", "revenue"],
            rows=[("N", Decimal("-60"))],
            row_count=1,
            truncated=True,
        )
        assert report.status == "failed"
        assert {check.name for check in report.failed} >= {
            "non_negative_measures",
            "complete_result_set",
        }
