"""Controlled vocabularies for the warehouse.

These are declared as `StrEnum` for type safety on the generation side, and
enforced in the database as `CHECK (col IN (...))` constraints rather than
native PostgreSQL `ENUM` types. Two reasons:

1.  Generated SQL stays simple. A native enum needs an explicit cast in several
    contexts (`= 'Enterprise'::customer_segment`), which is an easy thing for a
    language model to get wrong and a pointless source of retries.
2.  The permitted values remain readable in `pg_constraint`, so Phase 4's schema
    retrieval can show the model exactly which literals are valid — which is the
    single most effective way to stop it inventing `'ENTERPRISE'` or `'B2B'`.
"""

from __future__ import annotations

from enum import StrEnum


class CustomerSegment(StrEnum):
    """How a customer is served commercially."""

    ENTERPRISE = "Enterprise"
    SMB = "SMB"
    CONSUMER = "Consumer"


class AcquisitionChannel(StrEnum):
    """Where a customer originally came from."""

    ORGANIC = "Organic"
    PAID_SEARCH = "Paid Search"
    REFERRAL = "Referral"
    SOCIAL = "Social"
    PARTNER = "Partner"
    DIRECT = "Direct"


class AgeBand(StrEnum):
    """Coarse age bucket.

    Deliberately a band rather than a date of birth: it answers every
    demographic question the warehouse needs without storing a direct
    identifier for a (synthetic) person.
    """

    B18_24 = "18-24"
    B25_34 = "25-34"
    B35_44 = "35-44"
    B45_54 = "45-54"
    B55_64 = "55-64"
    B65_PLUS = "65+"


class OrderStatus(StrEnum):
    """Lifecycle state of an order.

    `completed` and `returned` both represent orders that shipped; `returned`
    came back. Revenue analysis should therefore normally count `completed`
    only — a distinction that makes return-rate questions meaningful.
    """

    COMPLETED = "completed"
    PENDING = "pending"
    CANCELLED = "cancelled"
    RETURNED = "returned"


class SalesChannel(StrEnum):
    """The route through which an order was placed."""

    WEB = "Web"
    MOBILE_APP = "Mobile App"
    PARTNER = "Partner"
    DIRECT_SALES = "Direct Sales"


class Department(StrEnum):
    """Employee organisational unit."""

    SALES = "Sales"
    CUSTOMER_SUCCESS = "Customer Success"
    MARKETING = "Marketing"
    OPERATIONS = "Operations"
    FINANCE = "Finance"
    ENGINEERING = "Engineering"


class RefundReason(StrEnum):
    """Why money went back to a customer.

    A refund is not a single phenomenon: "Damaged" and "Faulty" point at
    fulfilment and quality respectively, while "Changed mind" is demand-side
    and largely unpreventable. Collapsing them into one bucket makes a refund
    spike uninvestigable, which is the opposite of what this warehouse is for.
    """

    DAMAGED_IN_TRANSIT = "Damaged in transit"
    FAULTY = "Faulty"
    NOT_AS_DESCRIBED = "Not as described"
    WRONG_ITEM_SENT = "Wrong item sent"
    SIZE_OR_FIT = "Size or fit"
    LATE_DELIVERY = "Late delivery"
    CHANGED_MIND = "Changed mind"
    GOODWILL = "Goodwill"


def values(enum_cls: type[StrEnum]) -> list[str]:
    """Return an enum's members as plain strings, in declaration order.

    Used to build CHECK constraints and to seed generator weight tables.
    """
    return [member.value for member in enum_cls]
