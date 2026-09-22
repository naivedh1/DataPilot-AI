"""The semantic schema catalogue.

A column name is not a business definition. `orders.total_amount` exists in the
database, but "revenue" does not — and the difference between
`SUM(total_amount)` and `SUM(total_amount) WHERE status = 'completed'` is the
entire return rate. A model given only column names will silently pick one.

This module carries the layer the database cannot: what each table means, what
each column is for, which literal values are valid, and how the business metrics
are actually defined. It is hand-written on purpose. Generated descriptions
("the customer_segment column stores the customer segment") add tokens and no
information.

Structure is introspected live from SQLAlchemy metadata in `introspect.py`; this
file supplies only the meaning. The two are joined at retrieval time, so a column
described here that no longer exists is caught by a test rather than shipped to
the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ColumnDoc:
    """Business meaning of a single column."""

    description: str
    #: Extra search terms that should match this column. Not shown to the model.
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TableDoc:
    """Business meaning of a table and its columns."""

    description: str
    #: Terms a question might use to refer to this table.
    synonyms: tuple[str, ...] = ()
    columns: dict[str, ColumnDoc] = field(default_factory=dict)
    #: Guidance that only matters when this table is in play.
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MetricDoc:
    """A business metric with an unambiguous SQL definition.

    `expression` is a real SQL fragment, not prose. Supplying the definition is
    what stops the model inventing its own — and it is the difference between an
    answer that is merely plausible and one that is correct.
    """

    name: str
    description: str
    expression: str
    required_tables: tuple[str, ...]
    synonyms: tuple[str, ...] = ()
    caveat: str | None = None


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

TABLES: dict[str, TableDoc] = {
    "orders": TableDoc(
        description=(
            "One row per customer purchase. The central fact table: nearly every "
            "revenue, volume or trend question starts here."
        ),
        synonyms=("order", "purchase", "sale", "sales", "transaction", "revenue"),
        columns={
            "id": ColumnDoc("Primary key."),
            "order_number": ColumnDoc("Human-readable reference, e.g. ORD-202511-0001234."),
            "customer_id": ColumnDoc("The purchasing customer.", ("customer", "buyer")),
            "order_date": ColumnDoc(
                "Date the order was placed. Day granularity. Use date_trunc for "
                "monthly/quarterly roll-ups.",
                ("date", "when", "time", "month", "year", "quarter", "period"),
            ),
            "status": ColumnDoc(
                "Lifecycle state. 'completed' and 'returned' both shipped; "
                "'returned' came back. 'cancelled' never shipped. 'pending' is "
                "not yet resolved and only occurs for very recent orders.",
                ("state", "cancelled", "returned", "completed", "pending"),
            ),
            "sales_channel": ColumnDoc(
                "Route the order was placed through.", ("channel", "web", "mobile")
            ),
            "shipping_region_id": ColumnDoc(
                "Region the order shipped to. Usually, but not always, the "
                "customer's own region — use this for geography questions about "
                "orders, and customers.region_id for questions about customers.",
                ("region", "geography", "location", "territory"),
            ),
            "subtotal": ColumnDoc(
                "Sum of line totals before order-level discount, tax and shipping."
            ),
            "discount_amount": ColumnDoc(
                "Order-level promotion, on top of any per-line discounts."
            ),
            "tax_amount": ColumnDoc("Sales tax, at the shipping region's rate."),
            "shipping_amount": ColumnDoc(
                "Delivery charge. Zero above the free-shipping threshold."
            ),
            "total_amount": ColumnDoc(
                "What the customer paid: subtotal - discount + tax + shipping. "
                "This is the revenue column.",
                ("revenue", "sales", "value", "amount", "total", "turnover"),
            ),
            "created_at": ColumnDoc(
                "When the row was loaded into the warehouse. Not a business date."
            ),
        },
        notes=(
            "Revenue questions should normally filter status = 'completed'. "
            "Including 'returned' overstates revenue by the return rate; "
            "including 'cancelled' counts orders that never shipped.",
        ),
    ),
    "order_items": TableDoc(
        description=(
            "One row per product line within an order. Required for any "
            "product-level or category-level question."
        ),
        synonyms=("line", "line item", "basket", "item", "units", "quantity"),
        columns={
            "id": ColumnDoc("Primary key."),
            "order_id": ColumnDoc("Parent order."),
            "product_id": ColumnDoc("Product sold on this line.", ("product", "sku", "item")),
            "quantity": ColumnDoc("Units sold on this line.", ("units", "volume", "count")),
            "unit_price": ColumnDoc(
                "Price actually charged per unit, snapshotted at order time. May "
                "differ from products.unit_price, which is the current list price."
            ),
            "discount_amount": ColumnDoc("Discount applied to this line."),
            "line_total": ColumnDoc(
                "quantity * unit_price - discount_amount. Use this for "
                "product-level revenue, not products.unit_price.",
                ("revenue", "line revenue", "product revenue"),
            ),
        },
        notes=(
            "Product revenue is SUM(order_items.line_total), joined to orders to "
            "filter on status. Summing orders.total_amount after joining to "
            "order_items multiplies each order's total by its line count.",
        ),
    ),
    "refunds": TableDoc(
        description=(
            "One row per refund issued against an order. An order may be "
            "refunded more than once, on different dates and for different "
            "reasons, so refunds must be SUMmed per order, never assumed to "
            "be one row."
        ),
        synonyms=("refund", "refunds", "money back", "chargeback", "credit", "return"),
        columns={
            "id": ColumnDoc("Primary key."),
            "order_id": ColumnDoc("The order being refunded."),
            "refund_date": ColumnDoc(
                "When the refund was issued. This is later than the order date, "
                "often in a different month, so refunds belong to the period "
                "they were issued in, not the period of the original sale.",
                ("refunded on", "refund month", "when refunded"),
            ),
            "refund_amount": ColumnDoc(
                "Money returned on this refund row.",
                ("refunded", "refund value", "money back"),
            ),
            "refund_reason": ColumnDoc(
                "Why the refund happened. Distinguishes preventable causes "
                "(Damaged in transit, Faulty, Wrong item sent) from demand-side "
                "ones (Changed mind, Size or fit).",
                ("reason", "refund reason", "why"),
            ),
        },
        notes=(
            "A refund can be partial. orders.status = 'returned' means the "
            "refunds for that order sum to its total_amount; a partially "
            "refunded order is still 'completed' and still has refund rows. "
            "Counting 'returned' orders therefore undercounts refunds.",
            "Attribute refunds by refund_date unless the question explicitly "
            "asks about the period of the original sale.",
        ),
    ),
    "customers": TableDoc(
        description="One row per customer account, with segment and acquisition attributes.",
        synonyms=("customer", "client", "account", "buyer", "user"),
        columns={
            "id": ColumnDoc("Primary key."),
            "customer_code": ColumnDoc("Human-readable reference, e.g. CUS-001234."),
            "first_name": ColumnDoc("Contact first name."),
            "last_name": ColumnDoc("Contact surname."),
            "email": ColumnDoc("Contact email. Unique."),
            "signup_date": ColumnDoc(
                "When the customer registered. Use for acquisition cohorts.",
                ("acquisition", "joined", "registered", "cohort", "signup"),
            ),
            "region_id": ColumnDoc("The customer's home region.", ("region", "location")),
            "customer_segment": ColumnDoc(
                "Commercial segment. Drives order frequency and basket size.",
                ("segment", "tier", "enterprise", "smb", "consumer"),
            ),
            "acquisition_channel": ColumnDoc(
                "Where the customer originally came from.",
                ("channel", "source", "marketing", "referral", "organic", "paid"),
            ),
            "age_band": ColumnDoc("Coarse age bucket.", ("age", "demographic")),
            "is_active": ColumnDoc("Whether the account is currently active."),
            "created_at": ColumnDoc("Warehouse load timestamp. Not a business date."),
        },
    ),
    "products": TableDoc(
        description="One row per sellable item, with category, pricing and lifecycle.",
        synonyms=("product", "sku", "item", "catalogue", "catalog", "category"),
        columns={
            "id": ColumnDoc("Primary key."),
            "sku": ColumnDoc("Stock keeping unit, e.g. CE-00046."),
            "name": ColumnDoc("Display name."),
            "category": ColumnDoc(
                # "department" is deliberately NOT a synonym here: it collides
                # with employees.department and made staffing questions retrieve
                # the products table.
                "Top-level product grouping.",
                ("category", "product type", "product line"),
            ),
            "subcategory": ColumnDoc("Second-level grouping within a category."),
            "unit_price": ColumnDoc(
                "Current list price. For revenue use order_items.line_total, "
                "which reflects what was actually charged."
            ),
            "cost_price": ColumnDoc(
                "Unit cost. Margin = line_total - quantity * cost_price.",
                ("cost", "margin", "profit", "cogs"),
            ),
            "launch_date": ColumnDoc(
                "When the product went on sale.", ("launch", "new", "released")
            ),
            "is_active": ColumnDoc("False for discontinued lines.", ("discontinued", "retired")),
            "created_at": ColumnDoc("Warehouse load timestamp. Not a business date."),
        },
    ),
    "regions": TableDoc(
        description="Sales regions, grouped into territories. A small lookup table.",
        synonyms=("region", "territory", "geography", "country", "market", "location"),
        columns={
            "id": ColumnDoc("Primary key."),
            "name": ColumnDoc("Region name, e.g. 'Nordics'.", ("region",)),
            "country": ColumnDoc("Country the region sits in."),
            "territory": ColumnDoc(
                "Super-region roll-up: North America, EMEA or APAC.",
                ("territory", "continent", "area"),
            ),
            "created_at": ColumnDoc("Warehouse load timestamp."),
        },
    ),
    "employees": TableDoc(
        description=(
            "Staff roster by department and region. Deliberately NOT linked to "
            "orders — there is no sales-rep attribution in this warehouse, so "
            "questions tying employees to revenue cannot be answered."
        ),
        synonyms=("employee", "staff", "headcount", "team", "department", "hire"),
        columns={
            "id": ColumnDoc("Primary key."),
            "employee_code": ColumnDoc("Human-readable reference, e.g. EMP-00042."),
            "name": ColumnDoc("Full name."),
            "department": ColumnDoc("Organisational unit.", ("department", "function")),
            "role": ColumnDoc("Job title.", ("role", "title", "position")),
            "region_id": ColumnDoc("Region the employee is based in."),
            "hire_date": ColumnDoc("Start date.", ("hired", "tenure", "joined")),
            "created_at": ColumnDoc("Warehouse load timestamp."),
        },
    ),
}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

METRICS: tuple[MetricDoc, ...] = (
    MetricDoc(
        name="revenue",
        description="Money received from completed orders.",
        expression="SUM(orders.total_amount) FILTER (WHERE orders.status = 'completed')",
        required_tables=("orders",),
        synonyms=("revenue", "sales", "turnover", "income", "takings", "gmv"),
        caveat=(
            "Excludes returned and cancelled orders. If a question explicitly "
            "asks about gross or booked revenue, drop the status filter and say so. "
            "Because fully refunded orders are 'returned', this already nets out "
            "full refunds — but NOT partial refunds, which sit on orders that are "
            "still 'completed'. Use net_revenue when the question is about money "
            "actually kept."
        ),
    ),
    MetricDoc(
        name="net_revenue",
        description="Completed revenue after deducting every refund, full or partial.",
        expression=(
            "SUM(orders.total_amount) FILTER (WHERE orders.status = 'completed') "
            "- COALESCE((SELECT SUM(r.refund_amount) FROM refunds r "
            "JOIN orders o2 ON o2.id = r.order_id WHERE o2.status = 'completed'), 0)"
        ),
        required_tables=("orders", "refunds"),
        synonyms=("net revenue", "net sales", "revenue after refunds", "money kept"),
        caveat=(
            "Differs from revenue only by partial refunds; full refunds are "
            "already excluded by the status filter. Deducting all refunds from "
            "revenue would double-count the full ones. The refund deduction must "
            "be aggregated separately, not joined — an order with two refunds "
            "would otherwise duplicate its total_amount."
        ),
    ),
    MetricDoc(
        name="product_revenue",
        description="Revenue attributed to individual products or categories.",
        expression="SUM(order_items.line_total)",
        required_tables=("order_items", "orders"),
        synonyms=("product revenue", "category revenue", "revenue by product"),
        caveat=(
            "Join to orders to filter status = 'completed'. Do not sum "
            "orders.total_amount across a join to order_items — that multiplies "
            "each order's total by its number of lines."
        ),
    ),
    MetricDoc(
        name="order_count",
        description="Number of orders placed.",
        expression="COUNT(DISTINCT orders.id)",
        required_tables=("orders",),
        synonyms=("orders", "order volume", "number of orders", "transactions"),
    ),
    MetricDoc(
        name="average_order_value",
        description="Mean value of a completed order.",
        expression="AVG(orders.total_amount) FILTER (WHERE orders.status = 'completed')",
        required_tables=("orders",),
        synonyms=("aov", "average order value", "basket size", "average spend"),
    ),
    MetricDoc(
        name="return_rate",
        description="Share of orders refunded in full.",
        expression=(
            "COUNT(*) FILTER (WHERE orders.status = 'returned')::numeric / NULLIF(COUNT(*), 0)"
        ),
        required_tables=("orders",),
        synonyms=("return rate", "returns", "fully returned"),
        caveat=(
            "Counts orders, not money, and only fully refunded ones. A "
            "partially refunded order is 'completed' and is not counted here. "
            "For money returned use refund_amount or refund_rate."
        ),
    ),
    MetricDoc(
        name="refund_amount",
        description="Total money returned to customers.",
        expression="SUM(refunds.refund_amount)",
        required_tables=("refunds",),
        synonyms=("refunds", "refunded", "money refunded", "refund value"),
        caveat=(
            "Group by refunds.refund_date, not orders.order_date, unless the "
            "question asks about the period of the original sale. Join to "
            "orders only when the question needs order attributes; the join "
            "is not needed to total refunds."
        ),
    ),
    MetricDoc(
        name="refund_rate",
        description="Money refunded as a share of gross completed sales.",
        expression=(
            "SUM(refunds.refund_amount) / NULLIF(SUM(orders.total_amount) "
            "FILTER (WHERE orders.status IN ('completed', 'returned')), 0)"
        ),
        required_tables=("refunds", "orders"),
        synonyms=("refund rate", "refund ratio", "refund percentage"),
        caveat=(
            "This is a value ratio, not a count ratio — it is not return_rate. "
            "The denominator includes returned orders, because an order that "
            "was fully refunded was still a sale that was made. Beware joining "
            "orders to refunds directly and then summing total_amount: an order "
            "with two refunds would be counted twice. Aggregate each side "
            "separately."
        ),
    ),
    MetricDoc(
        name="cancellation_rate",
        description="Share of orders that were cancelled before shipping.",
        expression=(
            "COUNT(*) FILTER (WHERE orders.status = 'cancelled')::numeric / NULLIF(COUNT(*), 0)"
        ),
        required_tables=("orders",),
        synonyms=("cancellation rate", "cancelled", "cancellations"),
    ),
    MetricDoc(
        name="gross_margin",
        description="Revenue less cost of goods, as a share of revenue.",
        expression=(
            "(SUM(order_items.line_total) - SUM(order_items.quantity * products.cost_price)) "
            "/ NULLIF(SUM(order_items.line_total), 0)"
        ),
        required_tables=("order_items", "products", "orders"),
        synonyms=("margin", "gross margin", "profit", "profitability"),
    ),
    MetricDoc(
        name="units_sold",
        description="Total units shipped.",
        expression="SUM(order_items.quantity)",
        required_tables=("order_items",),
        synonyms=("units", "quantity sold", "volume", "items sold"),
    ),
    MetricDoc(
        name="customer_count",
        description="Number of distinct customers.",
        expression="COUNT(DISTINCT customers.id)",
        required_tables=("customers",),
        synonyms=("customers", "customer count", "accounts", "buyers"),
    ),
    MetricDoc(
        name="new_customers",
        description="Customers acquired in a period, by signup date.",
        expression="COUNT(DISTINCT customers.id)",
        required_tables=("customers",),
        synonyms=("new customers", "acquisition", "signups", "cohort"),
        caveat="Group by date_trunc on customers.signup_date, not orders.order_date.",
    ),
    MetricDoc(
        name="repeat_rate",
        description="Share of customers who placed more than one completed order.",
        expression=("COUNT(*) FILTER (WHERE order_count > 1)::numeric / NULLIF(COUNT(*), 0)"),
        required_tables=("customers", "orders"),
        synonyms=("repeat rate", "repeat purchase", "retention", "loyalty"),
        caveat="Requires a per-customer order count subquery or CTE first.",
    ),
)


#: Joins the model should be told about, keyed by the pair of tables involved.
JOIN_PATHS: dict[tuple[str, str], str] = {
    ("orders", "customers"): "orders.customer_id = customers.id",
    ("orders", "regions"): "orders.shipping_region_id = regions.id",
    ("orders", "order_items"): "order_items.order_id = orders.id",
    ("order_items", "products"): "order_items.product_id = products.id",
    ("customers", "regions"): "customers.region_id = regions.id",
    ("employees", "regions"): "employees.region_id = regions.id",
}


def join_clause(left: str, right: str) -> str | None:
    """The ON clause connecting two tables, in either order."""
    return JOIN_PATHS.get((left, right)) or JOIN_PATHS.get((right, left))
