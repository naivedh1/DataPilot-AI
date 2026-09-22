"""Fact tables: what actually happened.

`orders` and `order_items` carry the measures. Everything else in the warehouse
exists to slice these two.

Financial integrity is enforced by the database, not merely by the generator.
Both derivation rules are expressed as CHECK constraints:

    order_items.line_total  = quantity * unit_price - discount_amount
    orders.total_amount     = subtotal - discount_amount + tax_amount
                              + shipping_amount

Because every monetary column is NUMERIC with two decimal places and `quantity`
is an integer, both identities hold *exactly* — there is no rounding slack to
absorb, and no tolerance is needed. An inconsistent row cannot be inserted at
all, which is a far stronger guarantee than a test that checks for one after
the fact.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Money, TimestampMixin
from app.models.enums import OrderStatus, RefundReason, SalesChannel, values

if TYPE_CHECKING:
    from app.models.dimensions import Customer, Product, Region


def _in_list(column: str, allowed: list[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in allowed)
    return f"{column} IN ({rendered})"


class Order(Base, TimestampMixin):
    """A single purchase.

    `order_date` is a DATE, not a timestamp: this warehouse answers questions at
    day granularity and coarser, and a date column keeps `date_trunc` and
    range predicates straightforward for generated SQL.

    Note that `status` separates `completed` from `returned`. Revenue analysis
    should normally restrict to `completed`; treating the two as equivalent
    silently inflates revenue by the return rate.

    `returned` is *derived*, not independent: an order is `returned` exactly
    when its refunds sum to `total_amount`. A partially refunded order stays
    `completed` and carries refund rows for the part returned. The `refunds`
    table is therefore the source of truth for money returned, and `status`
    is a convenience denormalisation of the full-refund case. The equivalence
    is asserted after every load — see `seed/integrity.py`.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_number: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    customer_id: Mapped[int] = mapped_column(
        # RESTRICT: order history must survive any attempt to delete a customer.
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    order_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    sales_channel: Mapped[str] = mapped_column(String(16), nullable=False)
    shipping_region_id: Mapped[int] = mapped_column(
        ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False
    )

    subtotal: Mapped[Money] = mapped_column(nullable=False)
    discount_amount: Mapped[Money] = mapped_column(nullable=False)
    tax_amount: Mapped[Money] = mapped_column(nullable=False)
    shipping_amount: Mapped[Money] = mapped_column(nullable=False)
    total_amount: Mapped[Money] = mapped_column(nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="orders")
    shipping_region: Mapped[Region] = relationship(foreign_keys=[shipping_region_id])
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    refunds: Mapped[list[Refund]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(_in_list("status", values(OrderStatus)), name="status_known"),
        CheckConstraint(
            _in_list("sales_channel", values(SalesChannel)), name="sales_channel_known"
        ),
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        CheckConstraint("discount_amount >= 0", name="discount_non_negative"),
        CheckConstraint("tax_amount >= 0", name="tax_non_negative"),
        CheckConstraint("shipping_amount >= 0", name="shipping_non_negative"),
        CheckConstraint("total_amount >= 0", name="total_non_negative"),
        # A discount cannot exceed the goods it discounts.
        CheckConstraint("discount_amount <= subtotal", name="discount_within_subtotal"),
        # The definition of `total_amount`, enforced rather than assumed.
        CheckConstraint(
            "total_amount = subtotal - discount_amount + tax_amount + shipping_amount",
            name="total_is_consistent",
        ),
        # The single most important index here: essentially every analytical
        # query in this warehouse filters or groups by order_date.
        Index("ix_orders_order_date", "order_date"),
        # Join path from customers, and per-customer order history.
        Index("ix_orders_customer_id", "customer_id"),
        # Join path for regional revenue.
        Index("ix_orders_shipping_region_id", "shipping_region_id"),
        # Composite rather than a bare `status` index. `status` alone has four
        # distinct values, so on its own it is too low-cardinality for the
        # planner to prefer over a sequential scan. Led by status and followed
        # by order_date it directly serves the dominant access pattern —
        # "completed orders within a date range" — while its leading column
        # still covers plain status filters.
        Index("ix_orders_status_order_date", "status", "order_date"),
    )


class OrderItem(Base):
    """One product line within an order.

    `unit_price` is the price actually charged, snapshotted at order time. It is
    deliberately not a lookup through to `products.unit_price`: list prices
    change, and a historical order must keep reporting what the customer really
    paid.
    """

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        # CASCADE is correct here and only here: a line has no meaning without
        # its order, so deleting an order must take its lines with it rather
        # than leave orphaned rows that would corrupt every revenue total.
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[int] = mapped_column(
        # RESTRICT: a product with sales history must not be deletable.
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Money] = mapped_column(nullable=False)
    discount_amount: Mapped[Money] = mapped_column(nullable=False)
    line_total: Mapped[Money] = mapped_column(nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("discount_amount >= 0", name="discount_non_negative"),
        CheckConstraint("line_total >= 0", name="line_total_non_negative"),
        # The definition of `line_total`, enforced rather than assumed.
        CheckConstraint(
            "line_total = quantity * unit_price - discount_amount",
            name="line_total_is_consistent",
        ),
        # The join from orders. On the largest table in the warehouse this is
        # the difference between a sub-second aggregate and a full scan.
        Index("ix_order_items_order_id", "order_id"),
        # Product-level revenue and ranking questions.
        Index("ix_order_items_product_id", "product_id"),
    )


class Refund(Base):
    """Money returned to a customer against an order.

    Many-to-one with `orders`: a single order can be refunded more than once,
    for different reasons and on different dates. That is why this is a table
    and not a `refund_amount` column — a column cannot express "half of it came
    back damaged in March and the rest was a goodwill gesture in April", and
    flattening it loses the reason, which is what makes a refund spike
    diagnosable.

    Two invariants the database cannot express as CHECK constraints, because
    both span rows and tables:

        SUM(refunds.refund_amount) per order <= orders.total_amount
        orders.status = 'returned'  <=>  that sum equals total_amount

    They are enforced by the generator and verified after every load. A CHECK
    cannot see another table, and a trigger would slow COPY on the largest
    load in the warehouse for a guarantee the load already makes.
    """

    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        # CASCADE, as for order_items: a refund is meaningless without the
        # order it reverses.
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    refund_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    refund_amount: Mapped[Money] = mapped_column(nullable=False)
    refund_reason: Mapped[str] = mapped_column(String(32), nullable=False)

    order: Mapped[Order] = relationship(back_populates="refunds")

    __table_args__ = (
        # A zero-value refund is not a refund; a negative one is a charge.
        CheckConstraint("refund_amount > 0", name="refund_amount_positive"),
        CheckConstraint(
            _in_list("refund_reason", values(RefundReason)), name="refund_reason_known"
        ),
        # The join back to orders, and per-order refund totals.
        Index("ix_refunds_order_id", "order_id"),
        # "Refunds in August" is a first-class question, and refund_date is
        # deliberately not order_date — a refund lands in a later period than
        # the sale it reverses, which is exactly the subtlety worth testing.
        Index("ix_refunds_refund_date", "refund_date"),
        Index("ix_refunds_reason", "refund_reason"),
    )
