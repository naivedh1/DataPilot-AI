"""Declarative base and shared column types for the analytics warehouse.

A strict naming convention is applied to every constraint and index. Without it
PostgreSQL invents names like `orders_customer_id_fkey`, which are awkward to
assert against in tests and to reason about in migrations. With it, every object
name is derivable from the model definition.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from sqlalchemy import DateTime, MetaData, Numeric, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# pk / fk / uq / ck / ix names are deterministic and therefore assertable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

# ---------------------------------------------------------------------------
# Shared column types
# ---------------------------------------------------------------------------

# Money is NUMERIC, never float. Binary floating point cannot represent 0.10
# exactly, so sums of float money drift and the financial-integrity checks in
# tests would fail for reasons that have nothing to do with the data.
# 12,2 holds up to 9,999,999,999.99 — far beyond any single order here.
Money = Annotated[Decimal, mapped_column(Numeric(12, 2))]

# Unit economics carry four decimal places: a per-unit cost of 12.3456 is
# meaningful, and rounding it to cents before multiplying by quantity would
# introduce systematic margin error.
UnitMoney = Annotated[Decimal, mapped_column(Numeric(12, 4))]


class Base(DeclarativeBase):
    """Base class for every warehouse table."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Adds a server-assigned `created_at` audit column.

    Uses a server default rather than a Python default so that rows inserted by
    bulk COPY — which bypasses the ORM entirely — still get a value.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        doc="When this row was inserted into the warehouse.",
    )
