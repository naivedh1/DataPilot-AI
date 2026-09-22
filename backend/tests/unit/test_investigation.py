"""Tests for the diagnostic investigation.

The executor is injected, so the templates and the arithmetic are tested
without a database. What that buys is the ability to feed deliberately
inconsistent numbers — a breakdown that does not sum to its headline — which
is the case the reconciliation exists for and which real data will not produce
on demand.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from app.services import investigation as inv


def _plan(metric: str = "revenue", dimensions: list[str] | None = None) -> inv.InvestigationPlan:
    built = inv.build_plan(
        metric=metric, current_period="2026-02", dimensions=dimensions or ["region"]
    )
    assert built is not None
    return built


def _executor(responses: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> inv.Executor:
    """An executor that answers by matching a marker in the SQL."""

    def execute(sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        for marker, response in responses.items():
            if marker in sql:
                return response
        return [], []

    return execute


class TestPeriods:
    def test_parses_a_month(self):
        period = inv.parse_period("2026-02")
        assert period is not None
        assert period.start == dt.date(2026, 2, 1)
        assert period.end == dt.date(2026, 3, 1)

    def test_end_is_exclusive_across_a_year_boundary(self):
        period = inv.parse_period("2025-12")
        assert period is not None
        assert period.end == dt.date(2026, 1, 1)

    @pytest.mark.parametrize("bad", ["2026", "2026-13", "february", "", "2026-00", "x-y"])
    def test_rejects_nonsense(self, bad):
        assert inv.parse_period(bad) is None

    def test_preceding_month_crosses_january(self):
        assert inv.preceding_month(inv.month_period(2026, 1)).label == "2025-12"


class TestPlanValidation:
    def test_rejects_an_unknown_metric(self):
        """A plan naming a measure that does not exist must not be executed —
        it would produce an evidence trail of confident nonsense."""
        assert inv.build_plan(metric="profit_margin", current_period="2026-02") is None

    def test_rejects_a_period_outside_the_window(self):
        assert inv.build_plan(metric="revenue", current_period="2019-05") is None

    def test_defaults_the_comparison_to_the_preceding_month(self):
        plan = inv.build_plan(metric="revenue", current_period="2026-02")
        assert plan is not None
        assert plan.comparison.label == "2026-01"

    def test_drops_unknown_dimensions_but_keeps_valid_ones(self):
        plan = inv.build_plan(
            metric="revenue", current_period="2026-02", dimensions=["region", "weather"]
        )
        assert plan is not None
        assert plan.dimensions == ("region",)

    def test_falls_back_to_region_when_none_are_valid(self):
        plan = inv.build_plan(metric="revenue", current_period="2026-02", dimensions=["weather"])
        assert plan is not None
        assert plan.dimensions == ("region",)

    def test_revenue_plans_check_refunds_and_discounts(self):
        plan = _plan()
        assert "refund_impact" in plan.checks
        assert "discount_impact" in plan.checks


class TestTemplates:
    def test_totals_query_filters_both_periods(self):
        sql = inv.build_totals_sql(_plan())
        assert "2026-02-01" in sql and "2026-01-01" in sql
        assert "o.status = 'completed'" in sql

    def test_category_breakdown_uses_the_line_level_measure(self):
        """Summing an order total across its lines multiplies it by the line
        count. The line-level measure is the only correct one at that grain."""
        sql = inv.build_contribution_sql(_plan(dimensions=["category"]), "category")
        assert "SUM(oi.line_total)" in sql
        assert "SUM(o.total_amount)" not in sql

    def test_region_breakdown_keeps_the_order_level_measure(self):
        sql = inv.build_contribution_sql(_plan(), "region")
        assert "SUM(o.total_amount)" in sql

    def test_refund_impact_uses_refund_date(self):
        """A refund belongs to the period it was issued in, not the period of
        the sale it reverses."""
        sql = inv.build_refund_impact_sql(_plan())
        assert "r.refund_date" in sql
        assert "o.order_date" not in sql

    def test_every_template_is_a_single_select(self):
        plan = _plan(dimensions=["region", "category"])
        for sql in (
            inv.build_totals_sql(plan),
            inv.build_totals_sql(plan, line_level=True),
            inv.build_contribution_sql(plan, "region"),
            inv.build_refund_impact_sql(plan),
            inv.build_discount_impact_sql(plan),
        ):
            assert sql.lstrip().upper().startswith("SELECT")
            assert ";" not in sql


class TestArithmetic:
    def test_computes_change_and_percentage(self):
        executor = _executor(
            {
                "FROM orders o\nWHERE": (
                    ["current_value", "previous_value", "change"],
                    [(Decimal("80"), Decimal("100"), Decimal("-20"))],
                ),
                "GROUP BY": (
                    ["region", "current_value", "previous_value", "change"],
                    [("North", Decimal("30"), Decimal("45"), Decimal("-15"))],
                ),
            }
        )
        result = inv.run_investigation(_plan(), executor)
        assert result.change == -20.0
        assert result.percent_change == pytest.approx(-20.0)
        assert result.direction == "fell"

    def test_contributor_shares_are_signed_against_the_headline(self):
        """A group moving against the headline gets a negative share. "North
        fell but South grew" is the shape of most real diagnoses."""
        executor = _executor(
            {
                "FROM orders o\nWHERE": (
                    ["current_value", "previous_value", "change"],
                    [(Decimal("80"), Decimal("100"), Decimal("-20"))],
                ),
                "GROUP BY": (
                    ["region", "current_value", "previous_value", "change"],
                    [
                        ("North", Decimal("0"), Decimal("30"), Decimal("-30")),
                        ("South", Decimal("0"), Decimal("0"), Decimal("10")),
                    ],
                ),
            }
        )
        result = inv.run_investigation(_plan(), executor)
        shares = {c.label: c.share_of_change for c in result.contributors}
        assert shares["North"] == pytest.approx(1.5)
        assert shares["South"] == pytest.approx(-0.5)

    def test_no_percentage_when_the_previous_period_was_zero(self):
        executor = _executor(
            {
                "FROM orders o\nWHERE": (
                    ["current_value", "previous_value", "change"],
                    [(Decimal("80"), Decimal("0"), Decimal("80"))],
                )
            }
        )
        assert inv.run_investigation(_plan(), executor).percent_change is None


class TestReconciliation:
    def test_matching_breakdown_reconciles(self):
        executor = _executor(
            {
                "FROM orders o\nWHERE": (
                    ["current_value", "previous_value", "change"],
                    [(Decimal("80"), Decimal("100"), Decimal("-20"))],
                ),
                "GROUP BY": (
                    ["region", "current_value", "previous_value", "change"],
                    [("North", 0, 0, Decimal("-15")), ("South", 0, 0, Decimal("-5"))],
                ),
            }
        )
        assert inv.run_investigation(_plan(), executor).reconciled is True

    def test_a_breakdown_that_does_not_add_up_is_caught(self):
        executor = _executor(
            {
                "FROM orders o\nWHERE": (
                    ["current_value", "previous_value", "change"],
                    [(Decimal("80"), Decimal("100"), Decimal("-20"))],
                ),
                "GROUP BY": (
                    ["region", "current_value", "previous_value", "change"],
                    [("North", 0, 0, Decimal("-5"))],
                ),
            }
        )
        result = inv.run_investigation(_plan(), executor)
        assert result.reconciled is False
        assert any("do not sum" in caveat for caveat in result.caveats)

    def test_line_level_breakdown_reconciles_against_the_line_level_total(self):
        """A category split cannot equal order-level revenue: line values
        exclude tax, shipping and order-level discounts. Comparing the two
        would report that arithmetic as a defect."""
        executor = _executor(
            {
                "JOIN order_items oi ON oi.order_id = o.id\nWHERE": (
                    ["current_value", "previous_value", "change"],
                    [(Decimal("70"), Decimal("88"), Decimal("-18"))],
                ),
                "FROM orders o\nWHERE": (
                    ["current_value", "previous_value", "change"],
                    [(Decimal("80"), Decimal("100"), Decimal("-20"))],
                ),
                "GROUP BY": (
                    ["category", "current_value", "previous_value", "change"],
                    [("Apparel", 0, 0, Decimal("-18"))],
                ),
            }
        )
        result = inv.run_investigation(_plan(dimensions=["category"]), executor)
        assert result.reconciled is True
        assert any("tax and shipping" in caveat for caveat in result.caveats)

    def test_a_failed_step_does_not_crash_the_run(self):
        def executor(sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
            raise RuntimeError("relation does not exist")

        result = inv.run_investigation(_plan(), executor)
        assert all(step.error for step in result.steps)
        assert result.change == 0.0


class TestSerialisation:
    def test_plan_matches_the_documented_shape(self):
        payload = _plan(dimensions=["region", "category"]).to_dict()
        assert payload["intent"] == "diagnostic"
        assert payload["metric"] == "revenue"
        assert payload["current_period"] == "2026-02"
        assert payload["comparison_period"] == "2026-01"
        assert payload["dimensions_to_investigate"] == ["region", "category"]
        assert "reconciliation" in payload["checks"]

    def test_result_carries_every_step_it_ran(self):
        executor = _executor({"": ([], [])})
        payload = inv.run_investigation(_plan(), executor).to_dict()
        assert {step["name"] for step in payload["steps"]} >= {
            "period_totals",
            "contribution_by_region",
            "refund_impact",
            "discount_impact",
        }
        assert all(step["sql"] for step in payload["steps"])
