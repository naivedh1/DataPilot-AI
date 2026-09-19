"""Deliberate anomalies planted in the warehouse.

These exist so the agent has something real to find. Each is declared here once,
as data, which means:

* the generator applies them,
* Phase 13's evaluation imports the same declarations as ground truth,
* and the two can never drift apart.

**These declarations are never exposed through the API.** Nothing in `app/api`
or `app/agents` imports this module. An anomaly the application could simply
look up would not be testing analysis at all.

Magnitudes are chosen to be *investigable rather than obvious*. A 5-day outage
is stark in a daily series and nearly invisible in a monthly one; a regional
demand drop only shows up once revenue is split by region. Finding them requires
choosing the right granularity — which is the skill under test.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from app.database.seed.patterns import ProductArchetype


class AnomalyKind(StrEnum):
    """Categories of planted anomaly."""

    VOLUME_SPIKE = "volume_spike"
    VOLUME_DROP = "volume_drop"
    REGIONAL_DEMAND = "regional_demand"
    PRODUCT_COLLAPSE = "product_collapse"
    RETURN_RATE = "return_rate"


@dataclass(frozen=True, slots=True)
class WindowAnomaly:
    """An effect applied to every day in a closed date interval."""

    key: str
    kind: AnomalyKind
    start: dt.date
    end: dt.date
    multiplier: float
    description: str
    #: Region name for REGIONAL_DEMAND, category for RETURN_RATE, else None.
    target: str | None = None

    def covers(self, day: dt.date) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True, slots=True)
class ProductCollapseAnomaly:
    """A single product's demand falling off a cliff on a specific date.

    The target is described rather than named, because SKUs are generated. The
    generator resolves the description to one concrete product and reports which
    one, so the resolution is deterministic but not hard-coded.
    """

    key: str
    kind: AnomalyKind
    effective_from: dt.date
    category: str
    archetype: ProductArchetype
    residual_multiplier: float
    description: str


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

#: A one-week promotion. At monthly granularity March 2026 looks merely strong;
#: the spike is only obvious in a daily or weekly series.
FLASH_SALE = WindowAnomaly(
    key="flash_sale_2026_03",
    kind=AnomalyKind.VOLUME_SPIKE,
    start=dt.date(2026, 3, 16),
    end=dt.date(2026, 3, 22),
    multiplier=2.15,
    description=(
        "Unannounced seven-day promotion lifting order volume to roughly 2.1x "
        "baseline. Visible as a sharp spike in daily orders during mid-March "
        "2026; largely absorbed into the monthly total."
    ),
)

#: A short outage. Severe but brief — the classic case where monthly reporting
#: hides an incident entirely.
PLATFORM_OUTAGE = WindowAnomaly(
    key="platform_outage_2026_02",
    kind=AnomalyKind.VOLUME_DROP,
    start=dt.date(2026, 2, 9),
    end=dt.date(2026, 2, 13),
    multiplier=0.24,
    description=(
        "Five-day ordering outage cutting volume to roughly a quarter of "
        "baseline, 9-13 February 2026. Clear in a daily series, easy to miss "
        "in a monthly one."
    ),
)

#: A sustained regional slump. Invisible in the company total, obvious once
#: revenue is broken out by region.
REGIONAL_DISRUPTION = WindowAnomaly(
    key="nordics_disruption_2025_q4",
    kind=AnomalyKind.REGIONAL_DEMAND,
    start=dt.date(2025, 9, 1),
    end=dt.date(2025, 10, 31),
    multiplier=0.52,
    description=(
        "Logistics disruption in the Nordics region halving its order volume "
        "through September and October 2025. The company-wide total barely "
        "moves; the regional split makes it unmistakable."
    ),
    target="Nordics",
)

#: A quality problem surfacing as returns, confined to one category.
RETURN_RATE_SPIKE = WindowAnomaly(
    key="apparel_returns_2026_q1",
    kind=AnomalyKind.RETURN_RATE,
    start=dt.date(2026, 1, 1),
    end=dt.date(2026, 3, 31),
    multiplier=2.35,
    description=(
        "Apparel return rate roughly 2.3x its normal level across Q1 2026, "
        "consistent with a sizing or quality problem in a seasonal range. "
        "Order volume is unaffected, so only return-rate analysis reveals it."
    ),
    target="Apparel",
)

#: A sudden product-level collapse, distinct from the gradual decline that the
#: DECLINING archetype produces across the catalogue.
PRODUCT_COLLAPSE = ProductCollapseAnomaly(
    key="product_recall_2025_11",
    kind=AnomalyKind.PRODUCT_COLLAPSE,
    effective_from=dt.date(2025, 11, 15),
    category="Consumer Electronics",
    archetype=ProductArchetype.STAR,
    residual_multiplier=0.11,
    description=(
        "A consistently top-selling Consumer Electronics product loses close to "
        "90% of its demand overnight on 15 November 2025 and never recovers, "
        "consistent with a recall. Distinct from the catalogue's gradually "
        "declining products because the drop is abrupt and total."
    ),
)


WINDOW_ANOMALIES: tuple[WindowAnomaly, ...] = (
    FLASH_SALE,
    PLATFORM_OUTAGE,
    REGIONAL_DISRUPTION,
    RETURN_RATE_SPIKE,
)

ALL_ANOMALY_KEYS: tuple[str, ...] = (
    *(anomaly.key for anomaly in WINDOW_ANOMALIES),
    PRODUCT_COLLAPSE.key,
)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def volume_multiplier(day: dt.date) -> float:
    """Company-wide order-volume multiplier for a day."""
    multiplier = 1.0
    for anomaly in WINDOW_ANOMALIES:
        if anomaly.kind in (AnomalyKind.VOLUME_SPIKE, AnomalyKind.VOLUME_DROP) and anomaly.covers(
            day
        ):
            multiplier *= anomaly.multiplier
    return multiplier


def region_multiplier(region_name: str, day: dt.date) -> float:
    """Region-specific demand multiplier for a day."""
    multiplier = 1.0
    for anomaly in WINDOW_ANOMALIES:
        if (
            anomaly.kind is AnomalyKind.REGIONAL_DEMAND
            and anomaly.target == region_name
            and anomaly.covers(day)
        ):
            multiplier *= anomaly.multiplier
    return multiplier


def return_rate_multiplier(category: str, day: dt.date) -> float:
    """Category-specific return-rate multiplier for a day."""
    multiplier = 1.0
    for anomaly in WINDOW_ANOMALIES:
        if (
            anomaly.kind is AnomalyKind.RETURN_RATE
            and anomaly.target == category
            and anomaly.covers(day)
        ):
            multiplier *= anomaly.multiplier
    return multiplier
