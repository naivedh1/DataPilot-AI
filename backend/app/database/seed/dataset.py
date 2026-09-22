"""In-memory representation of a generated warehouse.

Dimension rows are held as small frozen dataclasses, because the generator needs
to read their attributes back while building orders (a customer's segment and
region drive how that customer behaves).

Fact rows are held as plain tuples. There are well over a hundred thousand of
them and they are written straight to PostgreSQL via COPY; wrapping each in an
object would cost memory and buy nothing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from app.database.seed.patterns import ProductArchetype

# Column orders for COPY. `created_at` is omitted throughout: it has a server
# default, so the database fills it in even though COPY bypasses the ORM.
REGION_COLUMNS = ("id", "name", "country", "territory")
PRODUCT_COLUMNS = (
    "id",
    "sku",
    "name",
    "category",
    "subcategory",
    "unit_price",
    "cost_price",
    "launch_date",
    "is_active",
)
CUSTOMER_COLUMNS = (
    "id",
    "customer_code",
    "first_name",
    "last_name",
    "email",
    "signup_date",
    "region_id",
    "customer_segment",
    "acquisition_channel",
    "age_band",
    "is_active",
)
EMPLOYEE_COLUMNS = (
    "id",
    "employee_code",
    "name",
    "department",
    "role",
    "region_id",
    "hire_date",
)
ORDER_COLUMNS = (
    "id",
    "order_number",
    "customer_id",
    "order_date",
    "status",
    "sales_channel",
    "shipping_region_id",
    "subtotal",
    "discount_amount",
    "tax_amount",
    "shipping_amount",
    "total_amount",
)
ORDER_ITEM_COLUMNS = (
    "id",
    "order_id",
    "product_id",
    "quantity",
    "unit_price",
    "discount_amount",
    "line_total",
)
REFUND_COLUMNS = (
    "id",
    "order_id",
    "refund_date",
    "refund_amount",
    "refund_reason",
)


@dataclass(frozen=True, slots=True)
class GenRegion:
    """A region, plus the commercial parameters used during generation."""

    id: int
    name: str
    country: str
    territory: str
    demand_weight: float
    tax_rate: Decimal

    def to_row(self) -> tuple[object, ...]:
        return (self.id, self.name, self.country, self.territory)


@dataclass(frozen=True, slots=True)
class GenProduct:
    """A product, plus its lifecycle archetype (which is not persisted)."""

    id: int
    sku: str
    name: str
    category: str
    subcategory: str
    unit_price: Decimal
    cost_price: Decimal
    launch_date: dt.date
    is_active: bool
    archetype: ProductArchetype

    def to_row(self) -> tuple[object, ...]:
        return (
            self.id,
            self.sku,
            self.name,
            self.category,
            self.subcategory,
            self.unit_price,
            self.cost_price,
            self.launch_date,
            self.is_active,
        )


@dataclass(frozen=True, slots=True)
class GenCustomer:
    """A customer, plus the ordering propensity derived from its attributes."""

    id: int
    customer_code: str
    first_name: str
    last_name: str
    email: str
    signup_date: dt.date
    region_id: int
    customer_segment: str
    acquisition_channel: str
    age_band: str
    is_active: bool
    #: Relative likelihood of placing any given order. Not persisted.
    order_weight: float

    def to_row(self) -> tuple[object, ...]:
        return (
            self.id,
            self.customer_code,
            self.first_name,
            self.last_name,
            self.email,
            self.signup_date,
            self.region_id,
            self.customer_segment,
            self.acquisition_channel,
            self.age_band,
            self.is_active,
        )


@dataclass(frozen=True, slots=True)
class GenEmployee:
    """A member of staff."""

    id: int
    employee_code: str
    name: str
    department: str
    role: str
    region_id: int
    hire_date: dt.date

    def to_row(self) -> tuple[object, ...]:
        return (
            self.id,
            self.employee_code,
            self.name,
            self.department,
            self.role,
            self.region_id,
            self.hire_date,
        )


@dataclass(frozen=True, slots=True)
class ResolvedAnomaly:
    """A planted anomaly, with whatever the generator resolved at runtime.

    Phase 13's evaluation uses these as ground truth. They are reported by the
    seeding command and are never served through the API.
    """

    key: str
    kind: str
    description: str
    detail: str


@dataclass(slots=True)
class GeneratedDataset:
    """Everything the generator produced, ready to load."""

    regions: list[GenRegion] = field(default_factory=list)
    products: list[GenProduct] = field(default_factory=list)
    customers: list[GenCustomer] = field(default_factory=list)
    employees: list[GenEmployee] = field(default_factory=list)
    orders: list[tuple[object, ...]] = field(default_factory=list)
    order_items: list[tuple[object, ...]] = field(default_factory=list)
    refunds: list[tuple[object, ...]] = field(default_factory=list)
    anomalies: list[ResolvedAnomaly] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """Row counts by table, in load order."""
        return {
            "regions": len(self.regions),
            "products": len(self.products),
            "customers": len(self.customers),
            "employees": len(self.employees),
            "orders": len(self.orders),
            "order_items": len(self.order_items),
            "refunds": len(self.refunds),
        }
