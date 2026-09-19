"""Analytical smoke tests.

These are the eight question shapes DataPilot AI exists to answer. They run as
`datapilot_readonly` — the same role and the same privileges the agent will have
— so they prove the read-only boundary does not get in the way of real analysis.

They assert on *shape and plausibility*, not on exact figures. Pinning
"November 2025 revenue was 2,431,904.55" would make every test a tripwire for
any generator change, without testing anything the agent depends on. What must
hold is that the query runs, returns the right shape, and that the business
patterns the warehouse claims to contain are actually present.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

pytestmark = pytest.mark.integration


def _query(conn: Any, sql: str) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


class TestRevenueOverTime:
    def test_monthly_revenue(self, readonly_conn, seeded):
        rows = _query(
            readonly_conn,
            """
            SELECT date_trunc('month', order_date)::date AS month,
                   SUM(total_amount) AS revenue,
                   COUNT(*) AS orders
            FROM orders WHERE status = 'completed'
            GROUP BY month ORDER BY month
            """,
        )
        assert len(rows) == 24, "expected 24 months of history"
        assert all(revenue > 0 for _, revenue, _ in rows)

    def test_monthly_revenue_growth(self, readonly_conn, seeded):
        """Month-over-month growth via a window function."""
        rows = _query(
            readonly_conn,
            """
            WITH monthly AS (
                SELECT date_trunc('month', order_date)::date AS month,
                       SUM(total_amount) AS revenue
                FROM orders WHERE status = 'completed' GROUP BY 1
            )
            SELECT month, revenue,
                   LAG(revenue) OVER (ORDER BY month) AS previous,
                   ROUND(100.0 * (revenue - LAG(revenue) OVER (ORDER BY month))
                         / NULLIF(LAG(revenue) OVER (ORDER BY month), 0), 2) AS pct
            FROM monthly ORDER BY month
            """,
        )
        assert len(rows) == 24
        assert rows[0][2] is None, "first month has no predecessor"
        changes = [row[3] for row in rows[1:] if row[3] is not None]
        assert any(change > 0 for change in changes)
        assert any(change < 0 for change in changes), "revenue never falls"

    def test_second_year_outgrows_the_first(self, readonly_conn, seeded):
        """The warehouse claims a ~22% annual growth trend. Verify it exists."""
        rows = _query(
            readonly_conn,
            """
            SELECT CASE WHEN order_date < '2025-09-01' THEN 'year_1' ELSE 'year_2' END,
                   SUM(total_amount)
            FROM orders WHERE status = 'completed' GROUP BY 1 ORDER BY 1
            """,
        )
        revenue = dict(rows)
        assert revenue["year_2"] > revenue["year_1"]

    def test_seasonality_is_visible_in_monthly_totals(self, readonly_conn, seeded):
        """November should outperform February — the warehouse's headline
        seasonal claim."""
        rows = _query(
            readonly_conn,
            """
            SELECT EXTRACT(MONTH FROM order_date)::int AS m, COUNT(*)
            FROM orders GROUP BY m ORDER BY m
            """,
        )
        by_month = dict(rows)
        assert by_month[11] > by_month[2], "November should beat February"


class TestRevenueBySegmentAndRegion:
    def test_revenue_by_region(self, readonly_conn, seeded):
        rows = _query(
            readonly_conn,
            """
            SELECT r.name, r.territory, SUM(o.total_amount) AS revenue, COUNT(*) AS orders
            FROM orders o
            JOIN regions r ON r.id = o.shipping_region_id
            WHERE o.status = 'completed'
            GROUP BY r.name, r.territory ORDER BY revenue DESC
            """,
        )
        assert len(rows) == 12
        revenues = [row[2] for row in rows]
        assert revenues == sorted(revenues, reverse=True)
        # Regions are deliberately unequal, or regional analysis is pointless.
        assert revenues[0] > revenues[-1] * 2

    def test_average_order_value_by_segment(self, readonly_conn, seeded):
        rows = _query(
            readonly_conn,
            """
            SELECT c.customer_segment, COUNT(*) AS orders,
                   ROUND(AVG(o.total_amount), 2) AS aov,
                   SUM(o.total_amount) AS revenue
            FROM orders o JOIN customers c ON c.id = o.customer_id
            WHERE o.status = 'completed'
            GROUP BY c.customer_segment ORDER BY aov DESC
            """,
        )
        assert len(rows) == 3
        by_segment = {row[0]: row for row in rows}
        assert by_segment["Enterprise"][2] > by_segment["SMB"][2]
        assert by_segment["SMB"][2] > by_segment["Consumer"][2]

    def test_consumers_dominate_order_count_but_not_revenue(self, readonly_conn, seeded):
        """A genuine analytical finding the agent should be able to reach:
        the largest segment by volume is not the largest by value."""
        rows = _query(
            readonly_conn,
            """
            SELECT c.customer_segment, COUNT(*), SUM(o.total_amount)
            FROM orders o JOIN customers c ON c.id = o.customer_id
            WHERE o.status = 'completed' GROUP BY 1
            """,
        )
        counts = {row[0]: row[1] for row in rows}
        revenue = {row[0]: row[2] for row in rows}
        assert max(counts, key=lambda k: counts[k]) == "Consumer"
        assert max(revenue, key=lambda k: revenue[k]) != "Consumer"

    def test_territory_rollup(self, readonly_conn, seeded):
        rows = _query(
            readonly_conn,
            """
            SELECT r.territory, SUM(o.total_amount)
            FROM orders o JOIN regions r ON r.id = o.shipping_region_id
            WHERE o.status = 'completed' GROUP BY 1 ORDER BY 2 DESC
            """,
        )
        assert {row[0] for row in rows} == {"North America", "EMEA", "APAC"}


class TestProductAnalysis:
    def test_top_ten_products_by_revenue(self, readonly_conn, seeded):
        rows = _query(
            readonly_conn,
            """
            SELECT p.sku, p.name, p.category, SUM(oi.line_total) AS revenue
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            JOIN products p ON p.id = oi.product_id
            WHERE o.status = 'completed'
            GROUP BY p.sku, p.name, p.category
            ORDER BY revenue DESC LIMIT 10
            """,
        )
        assert len(rows) == 10
        revenues = [row[3] for row in rows]
        assert revenues == sorted(revenues, reverse=True)
        assert all(revenue > 0 for revenue in revenues)

    def test_revenue_by_category(self, readonly_conn, seeded):
        rows = _query(
            readonly_conn,
            """
            SELECT p.category, SUM(oi.line_total) AS revenue, COUNT(DISTINCT o.id)
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            JOIN products p ON p.id = oi.product_id
            WHERE o.status = 'completed'
            GROUP BY p.category ORDER BY revenue DESC
            """,
        )
        assert len(rows) == 8, "all eight categories should have sales"

    def test_return_rate_by_category(self, readonly_conn, seeded):
        rows = _query(
            readonly_conn,
            """
            SELECT p.category,
                   COUNT(DISTINCT o.id) AS orders,
                   COUNT(DISTINCT o.id) FILTER (WHERE o.status = 'returned') AS returned,
                   ROUND(100.0 * COUNT(DISTINCT o.id)
                         FILTER (WHERE o.status = 'returned')
                         / NULLIF(COUNT(DISTINCT o.id), 0), 2) AS return_rate
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            JOIN products p ON p.id = oi.product_id
            GROUP BY p.category ORDER BY return_rate DESC
            """,
        )
        assert len(rows) == 8
        rates = {row[0]: row[3] for row in rows}
        # Apparel returns most (fit), industrial least (specified up front).
        assert rates["Apparel"] > rates["Industrial Equipment"]
        assert all(0 <= rate <= 100 for rate in rates.values())

    def test_margin_is_computable(self, readonly_conn, seeded):
        """`cost_price` exists so margin needs no second source."""
        rows = _query(
            readonly_conn,
            """
            SELECT p.category,
                   SUM(oi.line_total) AS revenue,
                   SUM(oi.quantity * p.cost_price) AS cost,
                   ROUND(100.0 * (SUM(oi.line_total) - SUM(oi.quantity * p.cost_price))
                         / NULLIF(SUM(oi.line_total), 0), 2) AS margin_pct
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            JOIN products p ON p.id = oi.product_id
            WHERE o.status = 'completed'
            GROUP BY p.category
            """,
        )
        assert len(rows) == 8
        assert all(row[3] is not None for row in rows)


class TestCustomerAnalysis:
    def test_customer_acquisition_by_channel(self, readonly_conn, seeded):
        rows = _query(
            readonly_conn,
            """
            SELECT acquisition_channel, COUNT(*) AS customers
            FROM customers GROUP BY 1 ORDER BY customers DESC
            """,
        )
        assert len(rows) == 6
        # Every customer lands in exactly one channel: the GROUP BY must not
        # drop or duplicate any.
        assert sum(row[1] for row in rows) == seeded.customers

    def test_channel_quality_differs(self, readonly_conn, seeded):
        """Acquisition source should predict customer value, or "which channel
        is best?" has no answer."""
        rows = _query(
            readonly_conn,
            """
            SELECT c.acquisition_channel,
                   COUNT(DISTINCT c.id) AS customers,
                   ROUND(COUNT(o.id)::numeric / COUNT(DISTINCT c.id), 3) AS orders_per_customer
            FROM customers c
            LEFT JOIN orders o ON o.customer_id = c.id AND o.status = 'completed'
            GROUP BY 1 ORDER BY orders_per_customer DESC
            """,
        )
        rates = [row[2] for row in rows]
        assert rates[0] > rates[-1] * Decimal("1.3"), "channels look identical"

    def test_monthly_signups(self, readonly_conn, seeded):
        rows = _query(
            readonly_conn,
            """
            SELECT date_trunc('month', signup_date)::date AS month, COUNT(*)
            FROM customers GROUP BY month ORDER BY month
            """,
        )
        assert len(rows) > 24

    def test_repeat_purchase_behaviour(self, readonly_conn, seeded):
        rows = _query(
            readonly_conn,
            """
            SELECT c.customer_segment,
                   COUNT(DISTINCT c.id) FILTER (WHERE order_count > 1) AS repeat_customers,
                   COUNT(DISTINCT c.id) AS total
            FROM customers c
            LEFT JOIN (
                SELECT customer_id, COUNT(*) AS order_count
                FROM orders WHERE status = 'completed' GROUP BY customer_id
            ) s ON s.customer_id = c.id
            GROUP BY c.customer_segment
            """,
        )
        assert len(rows) == 3
        assert all(row[1] > 0 for row in rows)


class TestOrderVolume:
    def test_monthly_order_volume(self, readonly_conn, seeded):
        rows = _query(
            readonly_conn,
            """
            SELECT date_trunc('month', order_date)::date AS month, COUNT(*)
            FROM orders GROUP BY month ORDER BY month
            """,
        )
        assert len(rows) == 24
        counts = [row[1] for row in rows]
        assert max(counts) > min(counts) * 1.3, "monthly volume looks flat"

    def test_order_status_distribution_is_uneven(self, readonly_conn, seeded):
        rows = _query(
            readonly_conn,
            "SELECT status, COUNT(*) FROM orders GROUP BY status ORDER BY 2 DESC",
        )
        assert len(rows) == 4
        counts = dict(rows)
        assert counts["completed"] > sum(v for k, v in counts.items() if k != "completed"), (
            "completed should dominate"
        )

    def test_sales_channel_mix_varies_by_segment(self, readonly_conn, seeded):
        """Enterprise buys through people, consumers through screens."""
        rows = _query(
            readonly_conn,
            """
            SELECT c.customer_segment, o.sales_channel, COUNT(*)
            FROM orders o JOIN customers c ON c.id = o.customer_id
            GROUP BY 1, 2
            """,
        )
        mix: dict[str, dict[str, int]] = {}
        for segment, channel, count in rows:
            mix.setdefault(segment, {})[channel] = count

        enterprise = mix["Enterprise"]
        consumer = mix["Consumer"]
        enterprise_direct = enterprise.get("Direct Sales", 0) / sum(enterprise.values())
        consumer_direct = consumer.get("Direct Sales", 0) / sum(consumer.values())
        assert enterprise_direct > consumer_direct * 3


class TestQueryPerformance:
    def test_headline_aggregate_uses_an_index(self, readonly_conn, seeded):
        """The date index must actually be used, not merely exist."""
        rows = _query(
            readonly_conn,
            """
            EXPLAIN (FORMAT TEXT)
            SELECT SUM(total_amount) FROM orders
            WHERE order_date BETWEEN '2025-11-01' AND '2025-11-30'
              AND status = 'completed'
            """,
        )
        plan = " ".join(row[0] for row in rows)
        assert "Index" in plan or "Bitmap" in plan, f"sequential scan: {plan}"

    def test_large_join_completes(self, readonly_conn, seeded):
        """The heaviest realistic join shape, across all six tables."""
        rows = _query(
            readonly_conn,
            """
            SELECT r.territory, c.customer_segment, p.category,
                   SUM(oi.line_total) AS revenue
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            JOIN customers c ON c.id = o.customer_id
            JOIN regions r ON r.id = o.shipping_region_id
            JOIN products p ON p.id = oi.product_id
            WHERE o.status = 'completed'
            GROUP BY r.territory, c.customer_segment, p.category
            ORDER BY revenue DESC LIMIT 20
            """,
        )
        assert len(rows) == 20
