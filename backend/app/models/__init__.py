"""SQLAlchemy ORM models for the synthetic business warehouse.

These describe the *analytics* schema (regions, products, customers, employees,
orders, order_items) and are the single source of truth for it: tables,
constraints and indexes are all created from this metadata. Phase 4's schema
retrieval indexes against the same metadata, so the description the language
model sees can never drift from what the database actually contains.
"""

from app.models.base import Base
from app.models.dimensions import Customer, Employee, Product, Region
from app.models.enums import (
    AcquisitionChannel,
    AgeBand,
    CustomerSegment,
    Department,
    OrderStatus,
    SalesChannel,
)
from app.models.facts import Order, OrderItem

#: Insert order. Parents precede children so foreign keys always resolve.
TABLE_LOAD_ORDER: tuple[str, ...] = (
    "regions",
    "products",
    "customers",
    "employees",
    "orders",
    "order_items",
)

__all__ = [
    "TABLE_LOAD_ORDER",
    "AcquisitionChannel",
    "AgeBand",
    "Base",
    "Customer",
    "CustomerSegment",
    "Department",
    "Employee",
    "Order",
    "OrderItem",
    "OrderStatus",
    "Product",
    "Region",
    "SalesChannel",
]
