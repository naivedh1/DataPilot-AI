"""Dimension tables: who, what and where.

These describe the entities that orders are measured against. They change
slowly, are small relative to the fact tables, and supply almost every
`GROUP BY` in the warehouse.
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
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Money, TimestampMixin, UnitMoney
from app.models.enums import (
    AcquisitionChannel,
    AgeBand,
    CustomerSegment,
    Department,
    values,
)

if TYPE_CHECKING:
    from app.models.facts import Order


def _in_list(column: str, allowed: list[str]) -> str:
    """Render a `col IN ('a','b')` CHECK body from a controlled vocabulary."""
    rendered = ", ".join(f"'{value}'" for value in allowed)
    return f"{column} IN ({rendered})"


class Region(Base, TimestampMixin):
    """A sales territory.

    Synthetic geography: the territory/region names are invented groupings, and
    `country` is only ever a country name. No addresses, no coordinates, no
    personal data.
    """

    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    country: Mapped[str] = mapped_column(String(64), nullable=False)
    territory: Mapped[str] = mapped_column(
        String(32), nullable=False, doc="Super-region roll-up, e.g. EMEA."
    )

    customers: Mapped[list[Customer]] = relationship(
        back_populates="region", foreign_keys="Customer.region_id"
    )

    __table_args__ = (
        # Territory-level roll-ups ("revenue by territory") are a common first
        # cut before drilling into individual regions.
        Index("ix_regions_territory", "territory"),
    )


class Product(Base, TimestampMixin):
    """A sellable item.

    `unit_price` is the list price; the price actually charged is recorded per
    line on `order_items`, because promotions and contract pricing mean the two
    legitimately differ. `cost_price` is carried so margin is computable without
    a second source.
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    subcategory: Mapped[str] = mapped_column(String(48), nullable=False)
    # 2dp: this price is copied onto order lines, where the line-total
    # identity requires exact cent arithmetic.
    unit_price: Mapped[Money] = mapped_column(nullable=False)
    # 4dp: unit cost genuinely carries sub-cent precision, and it never
    # participates in an enforced equality.
    cost_price: Mapped[UnitMoney] = mapped_column(nullable=False)
    launch_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))

    __table_args__ = (
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("cost_price >= 0", name="cost_price_non_negative"),
        # "Revenue by category", "top subcategories" — the most frequent product
        # roll-up. Composite because subcategory is almost always drilled into
        # from a category, and a leading-column index serves both queries.
        Index("ix_products_category_subcategory", "category", "subcategory"),
        # Cohort questions about newly launched products.
        Index("ix_products_launch_date", "launch_date"),
    )


class Customer(Base, TimestampMixin):
    """A buying account.

    `customer_segment` drives most behavioural differences in this warehouse —
    order frequency, basket size and average order value all vary by segment.
    """

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    first_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_name: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    signup_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    region_id: Mapped[int] = mapped_column(
        # RESTRICT: deleting a region must never silently delete its customers.
        ForeignKey("regions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    customer_segment: Mapped[str] = mapped_column(String(16), nullable=False)
    acquisition_channel: Mapped[str] = mapped_column(String(16), nullable=False)
    age_band: Mapped[str] = mapped_column(String(8), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))

    region: Mapped[Region] = relationship(back_populates="customers", foreign_keys=[region_id])
    orders: Mapped[list[Order]] = relationship(back_populates="customer")

    __table_args__ = (
        CheckConstraint(
            _in_list("customer_segment", values(CustomerSegment)),
            name="segment_known",
        ),
        CheckConstraint(
            _in_list("acquisition_channel", values(AcquisitionChannel)),
            name="channel_known",
        ),
        CheckConstraint(_in_list("age_band", values(AgeBand)), name="age_band_known"),
        # Regional customer counts, and the join from regions.
        Index("ix_customers_region_id", "region_id"),
        # "Average order value by segment" and every segment breakdown.
        Index("ix_customers_customer_segment", "customer_segment"),
        # Acquisition cohorts by month — the signup_date leading column also
        # serves plain date-range filters.
        Index("ix_customers_signup_date", "signup_date"),
        # "Acquisition by channel over time" is common enough to earn a
        # composite; channel alone is too low-cardinality to index usefully.
        Index("ix_customers_acquisition_channel_signup_date", "acquisition_channel", "signup_date"),
    )


class Employee(Base, TimestampMixin):
    """A member of staff, attached to a region.

    Intentionally not linked to orders. Adding a sales-rep foreign key would
    imply every order has an owning rep, which is false for self-serve web
    orders and would distort attribution analysis. This table supports headcount
    and organisational questions only.
    """

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    department: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    region_id: Mapped[int] = mapped_column(
        ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False
    )
    hire_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    region: Mapped[Region] = relationship(foreign_keys=[region_id])

    __table_args__ = (
        CheckConstraint(_in_list("department", values(Department)), name="department_known"),
        Index("ix_employees_region_id", "region_id"),
        Index("ix_employees_department", "department"),
    )
