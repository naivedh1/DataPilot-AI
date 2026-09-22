"""Order and order-line generation.

This is where the behavioural model in `patterns.py` and the anomalies in
`anomalies.py` actually compose.

The algorithm is deliberately two-stage:

1.  **When.** Build a daily demand curve (trend x month x weekday x anomalies),
    then allocate the configured order total across days in proportion to it.
2.  **Who.** For each order, pick a customer weighted by segment, acquisition
    channel and region — restricted to customers who had already signed up on
    that day.

Separating the two keeps each pattern independently inspectable, and means the
total order count is exact rather than the accumulated result of per-customer
draws.

Customer selection uses a prefix-sum over signup-sorted customers plus a binary
search, so choosing one of 5,000 eligible customers is O(log n) rather than
O(n). At 50,000 orders the naive version is roughly 250M operations; this is
about 600k.
"""

from __future__ import annotations

import bisect
import datetime as dt
import itertools
import random
from decimal import ROUND_HALF_UP, Decimal

from app.database.seed import anomalies as anomaly_rules
from app.database.seed.catalog import FREE_SHIPPING_THRESHOLD, SHIPPING_RATES
from app.database.seed.config import GenerationConfig
from app.database.seed.dataset import GenCustomer, GenProduct, GenRegion
from app.database.seed.patterns import (
    BASE_REFUND_RATE,
    CATEGORY_REFUND_BIAS,
    FULL_REFUND_SHARE,
    PARTIAL_REFUND_MAX_SHARE,
    PARTIAL_REFUND_MIN_SHARE,
    REFUND_DELAY_MAX_DAYS,
    REFUND_DELAY_MIN_DAYS,
    REFUND_DELAY_MODE_DAYS,
    REFUND_REASON_WEIGHTS,
    RESOLVED_STATUS_WEIGHTS,
    SECOND_REFUND_CHANCE,
    SEGMENT_PROFILES,
    ProductArchetype,
    base_intensity,
    category_multiplier,
    lifecycle_multiplier,
    segment_category_affinity,
)
from app.models.enums import CustomerSegment, OrderStatus, SalesChannel

CENTS = Decimal("0.01")

#: Sales-channel mix per segment. Enterprise buys through people; consumers
#: buy through screens.
CHANNEL_BY_SEGMENT: dict[str, dict[str, float]] = {
    CustomerSegment.ENTERPRISE: {
        SalesChannel.DIRECT_SALES: 0.62,
        SalesChannel.PARTNER: 0.28,
        SalesChannel.WEB: 0.09,
        SalesChannel.MOBILE_APP: 0.01,
    },
    CustomerSegment.SMB: {
        SalesChannel.WEB: 0.46,
        SalesChannel.DIRECT_SALES: 0.24,
        SalesChannel.PARTNER: 0.20,
        SalesChannel.MOBILE_APP: 0.10,
    },
    CustomerSegment.CONSUMER: {
        SalesChannel.WEB: 0.54,
        SalesChannel.MOBILE_APP: 0.40,
        SalesChannel.PARTNER: 0.04,
        SalesChannel.DIRECT_SALES: 0.02,
    },
}

#: Probability that a recent order is still unresolved.
PENDING_RATE_IN_RECENT_WINDOW = 0.42

#: Maximum redraws when the regional-anomaly filter rejects a customer.
_MAX_REGION_REDRAWS = 6


class _MonthlyProductWeights:
    """Per-month, per-segment product selection weights.

    Recomputing a 420-product weight vector for every one of ~140,000 order
    lines would dominate runtime. Product appeal only varies by calendar month
    (category seasonality, lifecycle position) and by buyer segment (price
    affinity), so there are only ~24 x 3 distinct vectors. They are computed
    once and sampled with a binary search.
    """

    def __init__(self, products: list[GenProduct], config: GenerationConfig) -> None:
        self._products = products
        self._config = config
        self._cache: dict[tuple[int, int, str], list[float]] = {}

    def cumulative(self, year: int, month: int, segment: str) -> list[float]:
        """Cumulative weight vector for a (month, segment) pair."""
        key = (year, month, segment)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        affinity = SEGMENT_PROFILES[segment].price_affinity
        month_midpoint = dt.date(year, month, 15)
        progress = min(
            max(self._config.day_index(month_midpoint) / self._config.total_days, 0.0),
            1.0,
        )

        cumulative: list[float] = []
        running = 0.0
        for product in self._products:
            if product.launch_date > month_midpoint:
                cumulative.append(running)  # not yet on sale
                continue

            weight = lifecycle_multiplier(product.archetype, progress)
            weight *= category_multiplier(product.category, month)
            if product.archetype is ProductArchetype.SEASONAL:
                # Seasonal lines swing harder than their category baseline.
                weight *= category_multiplier(product.category, month) ** 1.4
            # Price affinity: positive pulls a segment up-market.
            weight *= float(product.unit_price) ** affinity
            # Segment/category fit — the dominant term. Keeps industrial
            # equipment out of consumer baskets and t-shirts out of
            # enterprise ones.
            weight *= segment_category_affinity(segment, product.category)
            if not product.is_active:
                weight *= 0.12

            running += max(weight, 1e-9)
            cumulative.append(running)

        self._cache[key] = cumulative
        return cumulative

    def pick(self, rng: random.Random, year: int, month: int, segment: str) -> GenProduct:
        cumulative = self.cumulative(year, month, segment)
        target = rng.random() * cumulative[-1]
        index = bisect.bisect_left(cumulative, target)
        return self._products[min(index, len(self._products) - 1)]


def _resolve_collapsed_product(products: list[GenProduct]) -> GenProduct:
    """Find the product the collapse anomaly targets.

    Resolved by description rather than hard-coded SKU: the lowest-id STAR
    product in the declared category. Deterministic for a given seed, but not
    brittle if the catalogue size changes.
    """
    spec = anomaly_rules.PRODUCT_COLLAPSE
    candidates = [
        product
        for product in products
        if product.category == spec.category and product.archetype is spec.archetype
    ]
    if not candidates:
        # The catalogue builder guarantees this cannot happen. If it does, the
        # guarantee has been broken and the warehouse would ship without an
        # anomaly the evaluation suite expects to find — so fail rather than
        # generate data that quietly contradicts its own ground truth.
        raise ValueError(
            f"anomaly {spec.key!r} has no target: no {spec.archetype} product "
            f"in category {spec.category!r}"
        )
    return min(candidates, key=lambda p: p.id)


def _daily_order_counts(rng: random.Random, config: GenerationConfig) -> list[int]:
    """Allocate the configured order total across days by demand intensity."""
    weights = []
    for index in range(config.total_days):
        day = config.date_at(index)
        intensity = base_intensity(day, index, config.annual_growth)
        weights.append(intensity * anomaly_rules.volume_multiplier(day))

    chosen = rng.choices(range(config.total_days), weights=weights, k=config.n_orders)
    counts = [0] * config.total_days
    for day_index in chosen:
        counts[day_index] += 1
    return counts


def _pick_status(
    rng: random.Random,
    day: dt.date,
    config: GenerationConfig,
    dominant_category: str,
) -> str:
    """Choose an order status, before refunds are known.

    Recent orders may still be pending; older ones have necessarily resolved.

    This never returns `returned`. That status is decided by `_build_refunds`,
    which sets it when an order's refunds sum to its total — so the refunds
    table and the status can never disagree.
    """
    days_from_end = (config.window_end - day).days
    if days_from_end <= config.pending_window_days and rng.random() < PENDING_RATE_IN_RECENT_WINDOW:
        return str(OrderStatus.PENDING)

    keys = list(RESOLVED_STATUS_WEIGHTS)
    return rng.choices(keys, weights=[RESOLVED_STATUS_WEIGHTS[k] for k in keys], k=1)[0]


def _refund_date(
    rng: random.Random,
    config: GenerationConfig,
    not_before: dt.date,
) -> dt.date:
    """A refund date after `not_before`, clamped to the loaded window.

    Refunds land in a later period than the sale they reverse, which is the
    point: totalling refunds by order month rather than refund month is a real
    and easy analytical mistake, and the data has to be able to expose it.
    """
    delay = int(
        rng.triangular(REFUND_DELAY_MIN_DAYS, REFUND_DELAY_MAX_DAYS + 0.999, REFUND_DELAY_MODE_DAYS)
    )
    return min(not_before + dt.timedelta(days=delay), config.window_end)


def _build_refunds(
    *,
    rng: random.Random,
    config: GenerationConfig,
    refund_id: itertools.count,
    order_id: int,
    order_date: dt.date,
    total_amount: Decimal,
    dominant_category: str,
) -> tuple[list[tuple[object, ...]], bool]:
    """Decide an order's refunds. Returns `(rows, fully_refunded)`.

    `fully_refunded` is computed from the rows rather than chosen up front, so
    the `status = 'returned'` flag the caller sets is always exactly
    "the refunds add up to the total" — including when two partial refunds
    happen to close the order out.
    """
    probability = BASE_REFUND_RATE * CATEGORY_REFUND_BIAS.get(dominant_category, 1.0)
    probability *= anomaly_rules.return_rate_multiplier(dominant_category, order_date)
    if total_amount <= 0 or rng.random() >= min(probability, 0.95):
        return [], False

    first_date = _refund_date(rng, config, order_date)

    if rng.random() < FULL_REFUND_SHARE:
        row = (
            next(refund_id),
            order_id,
            first_date,
            total_amount,
            _weighted(rng, REFUND_REASON_WEIGHTS),
        )
        return [row], True

    share = Decimal(str(round(rng.uniform(PARTIAL_REFUND_MIN_SHARE, PARTIAL_REFUND_MAX_SHARE), 4)))
    amount = (total_amount * share).quantize(CENTS, rounding=ROUND_HALF_UP)
    if amount <= 0:
        return [], False

    rows: list[tuple[object, ...]] = [
        (next(refund_id), order_id, first_date, amount, _weighted(rng, REFUND_REASON_WEIGHTS))
    ]
    refunded = amount

    remaining = total_amount - refunded
    if remaining > 0 and rng.random() < SECOND_REFUND_CHANCE:
        second_share = Decimal(str(round(rng.uniform(0.3, 1.0), 4)))
        second = min((remaining * second_share).quantize(CENTS, rounding=ROUND_HALF_UP), remaining)
        if second > 0:
            rows.append(
                (
                    next(refund_id),
                    order_id,
                    _refund_date(rng, config, first_date),
                    second,
                    _weighted(rng, REFUND_REASON_WEIGHTS),
                )
            )
            refunded += second

    return rows, refunded == total_amount


def _quantity(rng: random.Random, low: int, high: int) -> int:
    """Draw a line quantity, skewed towards the low end of the segment's range."""
    if high <= low:
        return low
    # Triangular with the mode at the bottom: most lines are small even for
    # segments capable of large ones.
    return int(rng.triangular(low, high + 0.999, low))


def build_orders(
    rng: random.Random,
    config: GenerationConfig,
    customers: list[GenCustomer],
    products: list[GenProduct],
    regions: list[GenRegion],
) -> tuple[
    list[tuple[object, ...]],
    list[tuple[object, ...]],
    list[tuple[object, ...]],
    GenProduct,
]:
    """Generate all orders, their lines and their refunds.

    Returns `(order_rows, order_item_rows, refund_rows, collapsed_product)`.
    """
    regions_by_id = {region.id: region for region in regions}
    region_names = {region.id: region.name for region in regions}
    all_region_ids = [region.id for region in regions]

    collapsed_product = _resolve_collapsed_product(products)
    collapsed_id = collapsed_product.id
    collapse_from = anomaly_rules.PRODUCT_COLLAPSE.effective_from
    collapse_residual = anomaly_rules.PRODUCT_COLLAPSE.residual_multiplier

    weights = _MonthlyProductWeights(products, config)

    # Customers sorted by signup, with a prefix sum of ordering propensity.
    ordered = sorted(customers, key=lambda c: c.signup_date)
    signup_ordinals = [c.signup_date.toordinal() for c in ordered]
    prefix: list[float] = []
    running = 0.0
    for customer in ordered:
        running += customer.order_weight
        prefix.append(running)

    daily_counts = _daily_order_counts(rng, config)

    order_rows: list[tuple[object, ...]] = []
    item_rows: list[tuple[object, ...]] = []
    refund_rows: list[tuple[object, ...]] = []
    order_id = itertools.count(1)
    item_id = itertools.count(1)
    refund_id = itertools.count(1)
    sequence = itertools.count(1)

    for day_index, count in enumerate(daily_counts):
        if count == 0:
            continue
        day = config.date_at(day_index)
        day_ordinal = day.toordinal()
        eligible = bisect.bisect_right(signup_ordinals, day_ordinal)
        if eligible == 0:
            continue
        ceiling = prefix[eligible - 1]

        for _ in range(count):
            customer = _select_customer(rng, ordered, prefix, eligible, ceiling, region_names, day)
            profile = SEGMENT_PROFILES[customer.customer_segment]

            lines = _build_lines(
                rng=rng,
                day=day,
                customer=customer,
                weights=weights,
                collapsed_id=collapsed_id,
                collapse_from=collapse_from,
                collapse_residual=collapse_residual,
                profile_basket=(profile.basket_min, profile.basket_max),
                profile_quantity=(profile.quantity_min, profile.quantity_max),
                line_discount_rate=profile.line_discount_rate,
            )
            if not lines:
                continue

            current_order_id = next(order_id)
            subtotal = Decimal("0.00")
            category_totals: dict[str, Decimal] = {}

            for product, quantity, unit_price, discount, line_total in lines:
                item_rows.append(
                    (
                        next(item_id),
                        current_order_id,
                        product.id,
                        quantity,
                        unit_price,
                        discount,
                        line_total,
                    )
                )
                subtotal += line_total
                category_totals[product.category] = (
                    category_totals.get(product.category, Decimal("0.00")) + line_total
                )

            dominant_category = max(category_totals, key=lambda k: category_totals[k])

            # Order-level promotion, on top of any per-line discounts.
            if rng.random() < profile.order_discount_rate:
                rate = Decimal(str(round(rng.uniform(0.03, 0.12), 4)))
                order_discount = (subtotal * rate).quantize(CENTS, rounding=ROUND_HALF_UP)
                order_discount = min(order_discount, subtotal)
            else:
                order_discount = Decimal("0.00")

            shipping_region_id = customer.region_id
            if rng.random() < config.cross_region_shipping_rate:
                shipping_region_id = rng.choice(all_region_ids)

            taxable = subtotal - order_discount
            tax_rate = regions_by_id[shipping_region_id].tax_rate
            tax_amount = (taxable * tax_rate).quantize(CENTS, rounding=ROUND_HALF_UP)

            if taxable >= FREE_SHIPPING_THRESHOLD:
                shipping_amount = Decimal("0.00")
            else:
                shipping_amount = rng.choice(SHIPPING_RATES)

            # Exact by construction: every term is already 2dp, so the CHECK
            # constraint `total = subtotal - discount + tax + shipping` holds
            # with no rounding slack.
            total_amount = subtotal - order_discount + tax_amount + shipping_amount

            status = _pick_status(rng, day, config, dominant_category)

            # Only a fulfilled order can be refunded: there is nothing to
            # return on a cancelled one, and a pending one has not settled.
            if status == str(OrderStatus.COMPLETED):
                order_refunds, fully_refunded = _build_refunds(
                    rng=rng,
                    config=config,
                    refund_id=refund_id,
                    order_id=current_order_id,
                    order_date=day,
                    total_amount=total_amount,
                    dominant_category=dominant_category,
                )
                refund_rows.extend(order_refunds)
                if fully_refunded:
                    status = str(OrderStatus.RETURNED)

            channel = _weighted(rng, CHANNEL_BY_SEGMENT[customer.customer_segment])
            seq = next(sequence)

            order_rows.append(
                (
                    current_order_id,
                    f"ORD-{day.year}{day.month:02d}-{seq:07d}",
                    customer.id,
                    day,
                    status,
                    channel,
                    shipping_region_id,
                    subtotal,
                    order_discount,
                    tax_amount,
                    shipping_amount,
                    total_amount,
                )
            )

    return order_rows, item_rows, refund_rows, collapsed_product


def _weighted(rng: random.Random, weights: dict[str, float]) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _select_customer(
    rng: random.Random,
    ordered: list[GenCustomer],
    prefix: list[float],
    eligible: int,
    ceiling: float,
    region_names: dict[int, str],
    day: dt.date,
) -> GenCustomer:
    """Pick a weighted customer from those who had signed up by `day`.

    A regional demand anomaly is applied by rejection: a customer in an affected
    region is re-drawn with probability `1 - multiplier`. Rejection sampling
    keeps the fast prefix-sum path intact for the 99% of days with no anomaly,
    rather than rebuilding weight vectors per day.
    """
    for _ in range(_MAX_REGION_REDRAWS):
        target = rng.random() * ceiling
        index = bisect.bisect_left(prefix, target, 0, eligible)
        customer = ordered[min(index, eligible - 1)]

        multiplier = anomaly_rules.region_multiplier(region_names[customer.region_id], day)
        if multiplier >= 1.0 or rng.random() < multiplier:
            return customer

    return ordered[
        min(bisect.bisect_left(prefix, rng.random() * ceiling, 0, eligible), eligible - 1)
    ]


def _build_lines(
    *,
    rng: random.Random,
    day: dt.date,
    customer: GenCustomer,
    weights: _MonthlyProductWeights,
    collapsed_id: int | None,
    collapse_from: dt.date,
    collapse_residual: float,
    profile_basket: tuple[int, int],
    profile_quantity: tuple[int, int],
    line_discount_rate: float,
) -> list[tuple[GenProduct, int, Decimal, Decimal, Decimal]]:
    """Build the lines for one order.

    Returns tuples of (product, quantity, unit_price, discount, line_total).
    Every monetary value is a 2dp Decimal, so `line_total` is exact.
    """
    basket_min, basket_max = profile_basket
    n_lines = int(rng.triangular(basket_min, basket_max + 0.999, basket_min))
    n_lines = max(1, n_lines)

    seen: set[int] = set()
    lines: list[tuple[GenProduct, int, Decimal, Decimal, Decimal]] = []

    for _ in range(n_lines):
        product = None
        for _attempt in range(6):
            candidate = weights.pick(rng, day.year, day.month, customer.customer_segment)
            if candidate.id in seen:
                continue
            # The weight vector is cached per month and evaluates eligibility at
            # the month's midpoint, so a product launching mid-month is visible
            # to orders placed earlier that month. Re-check against the actual
            # order date, which is the only correct granularity.
            if candidate.launch_date > day:
                continue
            # The product-collapse anomaly, applied at day resolution rather
            # than month resolution so the drop lands on its exact date.
            if (
                candidate.id == collapsed_id
                and day >= collapse_from
                and rng.random() > collapse_residual
            ):
                continue
            product = candidate
            break
        if product is None:
            continue

        seen.add(product.id)
        quantity = _quantity(rng, *profile_quantity)
        unit_price = product.unit_price
        gross = unit_price * quantity

        if rng.random() < line_discount_rate:
            rate = Decimal(str(round(rng.uniform(0.05, 0.25), 4)))
            discount = (gross * rate).quantize(CENTS, rounding=ROUND_HALF_UP)
            discount = min(discount, gross)
        else:
            discount = Decimal("0.00")

        lines.append((product, quantity, unit_price, discount, gross - discount))

    return lines
