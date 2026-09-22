"""Reconciliation against the live warehouse.

The unit tests cover the arithmetic on hand-built rows. What can only be
tested here is the part that talks to the database: that a total query derived
from real SQL parses, runs, and produces a figure the grouped rows can be
compared against.
"""

from __future__ import annotations

import pytest

from app.database.executor import execute_readonly
from app.services.validation import (
    CheckStatus,
    ValidationCheck,
    build_total_query,
    validate_result,
)

pytestmark = pytest.mark.integration


def _reconcile(sql: str) -> ValidationCheck:
    """Run `sql`, derive and run its total, and return the reconciliation check."""
    result = execute_readonly(sql)
    total_sql = build_total_query(sql)
    assert total_sql is not None, "expected a derivable total query"
    total = execute_readonly(total_sql)

    report = validate_result(
        columns=result.columns,
        rows=result.rows,
        row_count=result.row_count,
        truncated=result.truncated,
        total_columns=total.columns,
        total_rows=total.rows,
    )
    return next(c for c in report.checks if c.name == "group_reconciliation")


class TestReconciliationAgainstTheWarehouse:
    def test_a_correct_grouped_query_reconciles(self, seeded):
        check = _reconcile(
            "SELECT rg.name AS region, SUM(o.total_amount) AS revenue "
            "FROM orders o JOIN regions rg ON rg.id = o.shipping_region_id "
            "WHERE o.status = 'completed' GROUP BY rg.name"
        )
        assert check.status is CheckStatus.PASSED

    def test_having_that_hides_groups_is_caught(self, seeded):
        """The failure this check exists for: an answer that omits groups its
        own total still counts. Every row shown is correct, and the total the
        user would compute from them is wrong.

        The threshold is derived from the data, not written in. A literal
        would exclude every category at CI's smaller scale, leaving an empty
        result that skips the check instead of failing it — the test would
        then pass locally and prove nothing anywhere.
        """
        by_category = execute_readonly(
            "SELECT SUM(oi.line_total) AS revenue FROM order_items oi "
            "JOIN products p ON p.id = oi.product_id GROUP BY p.category "
            "ORDER BY revenue DESC"
        )
        revenues = sorted(float(row[0]) for row in by_category.rows)
        assert len(revenues) >= 3, "need several groups for some to be excluded"
        # Median: roughly half the groups fall below it at any warehouse size.
        threshold = revenues[len(revenues) // 2]

        # The interpolated value is a float this test computed from its own
        # query, not anything a caller supplies.
        grouped = (
            "SELECT p.category AS category, SUM(oi.line_total) AS revenue "  # noqa: S608
            "FROM order_items oi JOIN products p ON p.id = oi.product_id "
            f"GROUP BY p.category HAVING SUM(oi.line_total) > {threshold}"
        )
        check = _reconcile(grouped)
        assert check.status is CheckStatus.FAILED
        assert "revenue" in check.detail

    def test_refunds_by_reason_reconciles(self, seeded):
        check = _reconcile(
            "SELECT refund_reason AS reason, SUM(refund_amount) AS refund_amount "
            "FROM refunds GROUP BY refund_reason"
        )
        assert check.status is CheckStatus.PASSED

    def test_a_top_n_query_is_not_reconciled(self, seeded):
        """Deliberately partial results must not be reported as a mismatch."""
        assert (
            build_total_query(
                "SELECT p.name, SUM(oi.line_total) AS revenue FROM order_items oi "
                "JOIN products p ON p.id = oi.product_id GROUP BY p.name "
                "ORDER BY revenue DESC LIMIT 5"
            )
            is None
        )

    def test_the_derived_total_matches_an_independently_written_one(self, seeded):
        """Guards the rewrite itself: if build_total_query dropped a WHERE
        clause, every reconciliation would silently compare the wrong things."""
        grouped = (
            "SELECT rg.name AS region, SUM(o.total_amount) AS revenue "
            "FROM orders o JOIN regions rg ON rg.id = o.shipping_region_id "
            "WHERE o.status = 'completed' GROUP BY rg.name"
        )
        derived = build_total_query(grouped)
        assert derived is not None

        by_hand = execute_readonly(
            "SELECT SUM(o.total_amount) AS revenue FROM orders o "
            "JOIN regions rg ON rg.id = o.shipping_region_id WHERE o.status = 'completed'"
        )
        assert execute_readonly(derived).rows[0][0] == by_hand.rows[0][0]
