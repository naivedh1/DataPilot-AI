"""The behavioural model behind the synthetic data.

Everything here is a **pure function of its arguments** — no randomness, no
clock, no database. That is deliberate: these functions encode the claims the
warehouse makes about itself ("November is the strongest month", "Enterprise
orders are larger and rarer"), and pure functions let those claims be unit
tested directly rather than inferred from generated output.

The generator supplies the randomness; this module supplies the shape.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from app.models.enums import AcquisitionChannel, CustomerSegment

# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

#: Monthly demand multipliers, January through December.
#:
#: Shaped like a consumer-retail business: a deep post-holiday trough, a long
#: flat middle, and a Q4 peak driven by November promotions running into
#: December gifting. Deliberately *not* symmetric and not centred on 1.0 —
#: real seasonality is lumpy.
MONTH_SEASONALITY: tuple[float, ...] = (
    0.83,  # Jan — post-holiday collapse
    0.80,  # Feb — the annual floor
    0.94,  # Mar
    0.99,  # Apr
    1.04,  # May
    1.01,  # Jun
    0.93,  # Jul — summer lull
    0.90,  # Aug
    1.06,  # Sep — back-to-work restock
    1.12,  # Oct
    1.38,  # Nov — the annual peak
    1.24,  # Dec
)

#: Monday through Sunday. Weekday-skewed: a mixed B2B/B2C book still does most
#: of its volume in the working week.
WEEKDAY_SEASONALITY: tuple[float, ...] = (
    1.09,  # Mon
    1.11,  # Tue
    1.07,  # Wed
    1.05,  # Thu
    0.98,  # Fri
    0.76,  # Sat
    0.71,  # Sun
)


def trend_multiplier(day_index: int, annual_growth: float) -> float:
    """Compound growth applied to a day's demand.

    Continuous compounding rather than a step per year, so the trend line is
    smooth and a year-over-year comparison at any point is meaningful.
    """
    if day_index < 0:
        raise ValueError("day_index must be non-negative")
    years = day_index / 365.25
    return (1.0 + annual_growth) ** years


def month_multiplier(day: dt.date) -> float:
    """Seasonal multiplier for the month `day` falls in."""
    return MONTH_SEASONALITY[day.month - 1]


def weekday_multiplier(day: dt.date) -> float:
    """Day-of-week multiplier. Monday is 0."""
    return WEEKDAY_SEASONALITY[day.weekday()]


def base_intensity(day: dt.date, day_index: int, annual_growth: float) -> float:
    """Relative order volume for a single day, before anomalies.

    The product of trend, month and weekday. Absolute scale is irrelevant: the
    generator normalises the whole curve to the configured order count, so only
    the ratios between days matter.
    """
    return (
        trend_multiplier(day_index, annual_growth) * month_multiplier(day) * weekday_multiplier(day)
    )


# ---------------------------------------------------------------------------
# Customer segments
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SegmentProfile:
    """How one customer segment behaves.

    `order_weight` is a *relative propensity to order*, not a count. The
    generator allocates a fixed total of orders across customers in proportion
    to it, so only the ratios matter.
    """

    share_of_customers: float
    order_weight: float
    basket_min: int
    basket_max: int
    quantity_min: int
    quantity_max: int
    #: Exponent applied to unit price when choosing products. Positive pulls a
    #: segment towards expensive items, negative towards cheap ones. This is
    #: what makes average order value differ by segment without hard-coding it.
    price_affinity: float
    #: Probability that any given line carries a discount.
    line_discount_rate: float
    #: Probability that the order carries an additional order-level promotion.
    order_discount_rate: float


SEGMENT_PROFILES: dict[str, SegmentProfile] = {
    # Few, infrequent, very large. Buys up-market.
    CustomerSegment.ENTERPRISE: SegmentProfile(
        share_of_customers=0.05,
        order_weight=0.45,
        basket_min=3,
        basket_max=9,
        quantity_min=2,
        quantity_max=14,
        price_affinity=0.20,
        line_discount_rate=0.55,
        order_discount_rate=0.30,
    ),
    # The commercial middle: moderate frequency, moderate baskets.
    CustomerSegment.SMB: SegmentProfile(
        share_of_customers=0.25,
        order_weight=0.78,
        basket_min=2,
        basket_max=6,
        quantity_min=1,
        quantity_max=6,
        price_affinity=-0.02,
        line_discount_rate=0.32,
        order_discount_rate=0.16,
    ),
    # Many, frequent, small. Price sensitive.
    CustomerSegment.CONSUMER: SegmentProfile(
        share_of_customers=0.70,
        order_weight=1.00,
        basket_min=1,
        basket_max=4,
        quantity_min=1,
        quantity_max=3,
        price_affinity=-0.46,
        line_discount_rate=0.18,
        order_discount_rate=0.09,
    ),
}


# ---------------------------------------------------------------------------
# Acquisition channels
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelProfile:
    """Acquisition channel mix and the customer quality it produces.

    `order_weight` encodes the well-known result that acquisition source
    predicts customer value: referred and partner-sourced customers order more
    than paid-social ones. This gives "which channel produces the best
    customers?" a real, discoverable answer.
    """

    share: float
    order_weight: float
    #: Relative likelihood of landing in each segment, Enterprise/SMB/Consumer.
    segment_bias: tuple[float, float, float]


CHANNEL_PROFILES: dict[str, ChannelProfile] = {
    AcquisitionChannel.REFERRAL: ChannelProfile(0.14, 1.34, (1.5, 1.4, 0.9)),
    AcquisitionChannel.PARTNER: ChannelProfile(0.09, 1.28, (3.0, 1.8, 0.5)),
    AcquisitionChannel.ORGANIC: ChannelProfile(0.28, 1.06, (1.0, 1.1, 1.0)),
    AcquisitionChannel.DIRECT: ChannelProfile(0.16, 1.00, (1.2, 1.0, 1.0)),
    AcquisitionChannel.PAID_SEARCH: ChannelProfile(0.21, 0.86, (0.6, 0.9, 1.1)),
    AcquisitionChannel.SOCIAL: ChannelProfile(0.12, 0.71, (0.2, 0.5, 1.3)),
}


# ---------------------------------------------------------------------------
# Segment x category affinity
# ---------------------------------------------------------------------------

#: How likely each segment is to buy from each category.
#:
#: This is what separates the segments' average order values, and it does so
#: for a *structural* reason rather than an arbitrary one: consumers do not buy
#: pallet trucks, and enterprises do not buy four t-shirts. Without it, every
#: segment drew from the whole catalogue and consumer baskets filled up with
#: industrial equipment priced in the thousands, producing an average consumer
#: order of several hundred pounds.
SEGMENT_CATEGORY_AFFINITY: dict[str, dict[str, float]] = {
    CustomerSegment.ENTERPRISE: {
        "Industrial Equipment": 3.2,
        "Software & Services": 3.0,
        "Office Supplies": 1.8,
        "Consumer Electronics": 1.1,
        "Health & Fitness": 0.30,
        "Home & Kitchen": 0.22,
        "Outdoor & Garden": 0.30,
        "Apparel": 0.12,
    },
    CustomerSegment.SMB: {
        "Office Supplies": 2.4,
        "Software & Services": 1.9,
        "Consumer Electronics": 1.4,
        "Industrial Equipment": 1.1,
        "Home & Kitchen": 0.85,
        "Outdoor & Garden": 0.80,
        "Health & Fitness": 0.55,
        "Apparel": 0.45,
    },
    CustomerSegment.CONSUMER: {
        "Home & Kitchen": 2.3,
        "Apparel": 2.2,
        "Consumer Electronics": 1.9,
        "Health & Fitness": 1.5,
        "Outdoor & Garden": 1.2,
        "Office Supplies": 0.70,
        "Software & Services": 0.16,
        "Industrial Equipment": 0.05,
    },
}


def segment_category_affinity(segment: str, category: str) -> float:
    """How strongly `segment` is drawn to `category`. 1.0 is neutral."""
    return SEGMENT_CATEGORY_AFFINITY.get(segment, {}).get(category, 1.0)


# ---------------------------------------------------------------------------
# Product lifecycle
# ---------------------------------------------------------------------------


class ProductArchetype(StrEnum):
    """How a product's popularity evolves across the window."""

    STAR = "star"  # consistently top-selling
    STEADY = "steady"  # flat, unremarkable
    SEASONAL = "seasonal"  # amplified category seasonality
    DECLINING = "declining"  # losing ground throughout
    GROWTH = "growth"  # launched recently, ramping


#: Mix of archetypes across the catalogue.
ARCHETYPE_SHARES: dict[ProductArchetype, float] = {
    ProductArchetype.STAR: 0.05,
    ProductArchetype.STEADY: 0.56,
    ProductArchetype.SEASONAL: 0.18,
    ProductArchetype.DECLINING: 0.11,
    ProductArchetype.GROWTH: 0.10,
}


def lifecycle_multiplier(archetype: ProductArchetype, progress: float) -> float:
    """Popularity multiplier at a point in the window.

    `progress` runs 0.0 at the window start to 1.0 at the end.

    Declining products lose roughly 70% of their pull across two years; growth
    products roughly triple. Both are gradual, so spotting them requires
    comparing periods rather than reading a single number — which is exactly
    the analytical work the agent should be doing.
    """
    if not 0.0 <= progress <= 1.0:
        raise ValueError("progress must be within [0, 1]")

    match archetype:
        case ProductArchetype.STAR:
            return 6.0
        case ProductArchetype.STEADY:
            return 1.0
        case ProductArchetype.SEASONAL:
            return 1.0  # seasonality is applied separately, by category
        case ProductArchetype.DECLINING:
            return 1.0 - 0.70 * progress
        case ProductArchetype.GROWTH:
            return 0.35 + 2.65 * progress


# ---------------------------------------------------------------------------
# Category seasonality
# ---------------------------------------------------------------------------

#: Per-category monthly multipliers layered on top of MONTH_SEASONALITY, so
#: categories peak at genuinely different times of year. Without this, every
#: category would be a scaled copy of the same curve and "which categories are
#: seasonal?" would have no answer.
CATEGORY_SEASONALITY: dict[str, tuple[float, ...]] = {
    # Gift-driven: enormous Q4, dead in Q1.
    "Consumer Electronics": (0.7, 0.7, 0.9, 0.9, 1.0, 1.0, 0.9, 0.9, 1.1, 1.3, 2.0, 1.7),
    # Summer peak, winter trough.
    "Outdoor & Garden": (0.4, 0.5, 1.0, 1.6, 2.0, 2.0, 1.7, 1.4, 0.9, 0.6, 0.4, 0.3),
    # Autumn/winter weighted.
    "Apparel": (0.8, 0.7, 0.9, 1.0, 1.0, 0.9, 0.8, 1.1, 1.4, 1.4, 1.5, 1.2),
    # Late-summer back-to-school spike.
    "Office Supplies": (1.2, 1.1, 1.1, 1.0, 0.9, 0.8, 1.0, 1.7, 1.6, 1.1, 1.0, 0.7),
    # January resolutions, then decay.
    "Health & Fitness": (1.9, 1.5, 1.3, 1.1, 1.0, 0.9, 0.8, 0.8, 1.0, 0.9, 0.9, 0.8),
    # B2B capital spend: flat, with a small year-end budget flush.
    "Industrial Equipment": (0.9, 1.0, 1.1, 1.0, 1.0, 1.1, 0.9, 0.9, 1.1, 1.1, 1.1, 1.2),
    # Non-seasonal staple.
    "Home & Kitchen": (1.0, 0.9, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.1, 1.1, 1.3, 1.2),
    # Steady B2B software/services.
    "Software & Services": (1.1, 1.0, 1.1, 1.0, 1.0, 1.0, 0.9, 0.9, 1.1, 1.1, 1.0, 1.0),
}


def category_multiplier(category: str, month: int) -> float:
    """Category-specific seasonal multiplier for a calendar month (1-12)."""
    if not 1 <= month <= 12:
        raise ValueError("month must be within 1..12")
    return CATEGORY_SEASONALITY.get(category, (1.0,) * 12)[month - 1]


# ---------------------------------------------------------------------------
# Order status
# ---------------------------------------------------------------------------

#: Baseline status mix for orders old enough to have resolved.
#: Deliberately uneven — a uniform split would make return-rate analysis
#: meaningless.
RESOLVED_STATUS_WEIGHTS: dict[str, float] = {
    "completed": 0.855,
    "cancelled": 0.079,
    "returned": 0.066,
}

#: Categories return at materially different rates. Apparel returns most
#: (fit), industrial least (specified before purchase).
CATEGORY_RETURN_BIAS: dict[str, float] = {
    "Apparel": 2.4,
    "Consumer Electronics": 1.45,
    "Health & Fitness": 1.2,
    "Home & Kitchen": 1.0,
    "Outdoor & Garden": 0.9,
    "Office Supplies": 0.6,
    "Software & Services": 0.35,
    "Industrial Equipment": 0.3,
}
