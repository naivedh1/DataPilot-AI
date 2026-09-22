"""Post-load integrity checks.

The CHECK constraints in `app/models/facts.py` already make an individually
inconsistent row unwritable. These checks cover what a per-row constraint
cannot: relationships *between* rows, most importantly that each order's
subtotal equals the sum of its own lines.

Every check returns a `CheckResult` rather than raising, so a seeding run
reports all problems at once instead of stopping at the first.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of a single integrity check."""

    name: str
    passed: bool
    detail: str

    @property
    def symbol(self) -> str:
        return "PASS" if self.passed else "FAIL"


def _scalar(conn: psycopg.Connection, query: str) -> object:
    """Run a query and return its first column of its first row."""
    with conn.cursor() as cur:
        cur.execute(query)
        row = cur.fetchone()
        return row[0] if row else None


def _count(conn: psycopg.Connection, query: str) -> int:
    """Run a COUNT query and return it as an int.

    The driver types a scalar result as `object`, so every call site would
    otherwise need its own narrowing. Doing it once here keeps the checks
    readable and keeps mypy satisfied without scattered casts.
    """
    value = _scalar(conn, query)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0
    return int(value)


def check_subtotals_match_lines(conn: psycopg.Connection) -> CheckResult:
    """Every order's subtotal must equal the sum of its own line totals.

    This is the check the database cannot enforce itself: a CHECK constraint
    sees one row, and this identity spans two tables.
    """
    mismatches = _count(
        conn,
        """
        SELECT count(*)
        FROM (
            SELECT o.id
            FROM orders o
            JOIN order_items oi ON oi.order_id = o.id
            GROUP BY o.id, o.subtotal
            HAVING o.subtotal <> SUM(oi.line_total)
        ) AS bad
        """,
    )
    count = mismatches
    return CheckResult(
        "order subtotal equals sum of its line totals",
        count == 0,
        "all orders consistent" if count == 0 else f"{count:,} mismatched orders",
    )


def check_totals_are_derived(conn: psycopg.Connection) -> CheckResult:
    """total_amount must equal subtotal - discount + tax + shipping."""
    mismatches = _count(
        conn,
        """
        SELECT count(*) FROM orders
        WHERE total_amount
              <> subtotal - discount_amount + tax_amount + shipping_amount
        """,
    )
    count = mismatches
    return CheckResult(
        "order total is correctly derived",
        count == 0,
        "all totals consistent" if count == 0 else f"{count:,} inconsistent totals",
    )


def check_line_totals_are_derived(conn: psycopg.Connection) -> CheckResult:
    """line_total must equal quantity * unit_price - discount."""
    mismatches = _count(
        conn,
        """
        SELECT count(*) FROM order_items
        WHERE line_total <> quantity * unit_price - discount_amount
        """,
    )
    count = mismatches
    return CheckResult(
        "line total is correctly derived",
        count == 0,
        "all lines consistent" if count == 0 else f"{count:,} inconsistent lines",
    )


def check_no_orphan_items(conn: psycopg.Connection) -> CheckResult:
    """Every order must have at least one line."""
    empty = _count(
        conn,
        """
        SELECT count(*) FROM orders o
        WHERE NOT EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id = o.id)
        """,
    )
    count = empty
    return CheckResult(
        "every order has at least one line",
        count == 0,
        "no empty orders" if count == 0 else f"{count:,} orders with no lines",
    )


def check_refunds_within_order_total(conn: psycopg.Connection) -> CheckResult:
    """No order may be refunded for more than it was worth.

    The database cannot express this as a CHECK — it spans rows and tables —
    so it is asserted here instead.
    """
    count = _count(
        conn,
        """
        SELECT count(*) FROM (
            SELECT r.order_id
            FROM refunds r
            JOIN orders o ON o.id = r.order_id
            GROUP BY r.order_id, o.total_amount
            HAVING SUM(r.refund_amount) > o.total_amount
        ) AS over_refunded
        """,
    )
    return CheckResult(
        "refunds never exceed the order total",
        count == 0,
        "all within total" if count == 0 else f"{count:,} over-refunded orders",
    )


def check_returned_status_matches_refunds(conn: psycopg.Connection) -> CheckResult:
    """`status = 'returned'` must mean exactly "refunds sum to the total".

    This is the invariant that makes `refunds` the source of truth rather than
    a second, competing account of what came back. Both directions are checked:
    a `returned` order that is not fully refunded, and a fully refunded order
    still marked `completed`, are equally wrong.
    """
    count = _count(
        conn,
        """
        SELECT count(*) FROM (
            SELECT o.id
            FROM orders o
            LEFT JOIN refunds r ON r.order_id = o.id
            GROUP BY o.id, o.status, o.total_amount
            HAVING (o.status = 'returned')
                <> (COALESCE(SUM(r.refund_amount), 0) = o.total_amount)
        ) AS mismatched
        """,
    )
    return CheckResult(
        "returned status matches refunds exactly",
        count == 0,
        "status and refunds agree" if count == 0 else f"{count:,} disagreeing orders",
    )


def check_refunds_not_before_orders(conn: psycopg.Connection) -> CheckResult:
    """Money cannot come back before it went out."""
    count = _count(
        conn,
        """
        SELECT count(*) FROM refunds r
        JOIN orders o ON o.id = r.order_id
        WHERE r.refund_date < o.order_date
        """,
    )
    return CheckResult(
        "no refund predates its order",
        count == 0,
        "all refunds follow their order" if count == 0 else f"{count:,} impossible refunds",
    )


def check_refunds_only_on_fulfilled_orders(conn: psycopg.Connection) -> CheckResult:
    """A cancelled or pending order has nothing to refund."""
    count = _count(
        conn,
        """
        SELECT count(*) FROM refunds r
        JOIN orders o ON o.id = r.order_id
        WHERE o.status NOT IN ('completed', 'returned')
        """,
    )
    return CheckResult(
        "refunds only against fulfilled orders",
        count == 0,
        "no refunds on unfulfilled orders" if count == 0 else f"{count:,} invalid refunds",
    )


def check_orders_after_signup(conn: psycopg.Connection) -> CheckResult:
    """No customer may have ordered before they existed."""
    impossible = _count(
        conn,
        """
        SELECT count(*) FROM orders o
        JOIN customers c ON c.id = o.customer_id
        WHERE o.order_date < c.signup_date
        """,
    )
    count = impossible
    return CheckResult(
        "no order predates its customer's signup",
        count == 0,
        "chronology intact" if count == 0 else f"{count:,} impossible orders",
    )


def check_no_presale_products(conn: psycopg.Connection) -> CheckResult:
    """No product may be sold before its launch date."""
    impossible = _count(
        conn,
        """
        SELECT count(*) FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE o.order_date < p.launch_date
        """,
    )
    count = impossible
    return CheckResult(
        "no product sold before launch",
        count == 0,
        "launch dates respected" if count == 0 else f"{count:,} pre-launch sales",
    )


def check_date_span(
    conn: psycopg.Connection, expected_start: dt.date, expected_end: dt.date
) -> CheckResult:
    """Orders must span approximately the configured window."""
    with conn.cursor() as cur:
        cur.execute("SELECT min(order_date), max(order_date) FROM orders")
        row = cur.fetchone()
    if not row or row[0] is None:
        return CheckResult("orders span the configured window", False, "no orders")

    first, last = row
    # A few empty days at either edge are expected: order counts are drawn, not
    # assigned, so the very first or last day can legitimately come up empty.
    tolerance = dt.timedelta(days=5)
    ok = abs(first - expected_start) <= tolerance and abs(last - expected_end) <= tolerance
    return CheckResult(
        "orders span the configured window",
        ok,
        f"{first} to {last} ({(last - first).days + 1} days)",
    )


def check_distribution_variety(conn: psycopg.Connection) -> CheckResult:
    """Segments, statuses, categories and regions must all be represented."""
    problems: list[str] = []
    expectations = {
        "customer segments": ("SELECT count(DISTINCT customer_segment) FROM customers", 3),
        "acquisition channels": ("SELECT count(DISTINCT acquisition_channel) FROM customers", 6),
        "order statuses": ("SELECT count(DISTINCT status) FROM orders", 4),
        "sales channels": ("SELECT count(DISTINCT sales_channel) FROM orders", 4),
        "product categories": ("SELECT count(DISTINCT category) FROM products", 8),
        "regions with orders": ("SELECT count(DISTINCT shipping_region_id) FROM orders", 12),
    }
    detail: list[str] = []
    for label, (query, expected) in expectations.items():
        actual = _count(conn, query)
        detail.append(f"{label}={actual}")
        if actual < expected:
            problems.append(f"{label}: {actual} < {expected}")

    return CheckResult(
        "categorical variety present",
        not problems,
        ", ".join(detail) if not problems else "; ".join(problems),
    )


def check_monthly_variation(conn: psycopg.Connection) -> CheckResult:
    """Monthly revenue must actually vary — flat data would be a generator bug."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT stddev_pop(monthly) / NULLIF(avg(monthly), 0)
            FROM (
                SELECT date_trunc('month', order_date) AS m, SUM(total_amount) AS monthly
                FROM orders WHERE status = 'completed' GROUP BY 1
            ) AS s
            """
        )
        row = cur.fetchone()
    coefficient = float(row[0]) if row and row[0] is not None else 0.0
    # Seasonality plus a 22%/yr trend should give well over 10% relative spread.
    return CheckResult(
        "monthly revenue varies (trend + seasonality present)",
        coefficient > 0.10,
        f"coefficient of variation = {coefficient:.3f}",
    )


def run_all(
    conn: psycopg.Connection, window_start: dt.date, window_end: dt.date
) -> list[CheckResult]:
    """Run every integrity check and return all results."""
    return [
        check_line_totals_are_derived(conn),
        check_totals_are_derived(conn),
        check_subtotals_match_lines(conn),
        check_no_orphan_items(conn),
        check_refunds_within_order_total(conn),
        check_returned_status_matches_refunds(conn),
        check_refunds_not_before_orders(conn),
        check_refunds_only_on_fulfilled_orders(conn),
        check_orders_after_signup(conn),
        check_no_presale_products(conn),
        check_date_span(conn, window_start, window_end),
        check_distribution_variety(conn),
        check_monthly_variation(conn),
    ]
