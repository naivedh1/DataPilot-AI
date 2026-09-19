"""Generation of the dimension rows: regions, products, customers, employees.

Every function here takes an explicit `random.Random`, never the module-level
`random`. Module-level randomness is process-global state that any other import
can disturb, which would silently destroy reproducibility.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import random
from decimal import ROUND_HALF_UP, Decimal

from faker import Faker

from app.database.seed.catalog import (
    CATEGORIES,
    CATEGORY_CODES,
    DEPARTMENT_ROLES,
    DEPARTMENT_SHARES,
    PRODUCT_NAME_PREFIXES,
    PRODUCT_NAME_SUFFIXES,
    REGIONS,
)
from app.database.seed.config import GenerationConfig
from app.database.seed.dataset import GenCustomer, GenEmployee, GenProduct, GenRegion
from app.database.seed.patterns import (
    ARCHETYPE_SHARES,
    CHANNEL_PROFILES,
    SEGMENT_PROFILES,
    ProductArchetype,
)
from app.models.enums import AgeBand, CustomerSegment

CENTS = Decimal("0.01")
SUB_CENTS = Decimal("0.0001")

#: Age-band mix. Skewed to the working-age middle, as a customer base is.
AGE_BAND_WEIGHTS: dict[str, float] = {
    AgeBand.B18_24: 0.11,
    AgeBand.B25_34: 0.26,
    AgeBand.B35_44: 0.24,
    AgeBand.B45_54: 0.19,
    AgeBand.B55_64: 0.13,
    AgeBand.B65_PLUS: 0.07,
}


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    """Pick one key in proportion to its weight.

    Iterates a dict, so it relies on insertion order being stable — which it is
    in Python 3.7+. That matters: a different iteration order would give a
    different result for the same seed.
    """
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def build_regions() -> list[GenRegion]:
    """Regions are fixed reference data, not randomly generated."""
    return [
        GenRegion(
            id=index,
            name=spec.name,
            country=spec.country,
            territory=spec.territory,
            demand_weight=spec.demand_weight,
            tax_rate=spec.tax_rate,
        )
        for index, spec in enumerate(REGIONS, start=1)
    ]


def _log_uniform_price(rng: random.Random, low: Decimal, high: Decimal) -> Decimal:
    """Draw a price log-uniformly within a band.

    Log-uniform rather than uniform because real catalogues are dense at the
    cheap end and sparse at the expensive end. A uniform draw over
    85-9400 would make mid-priced industrial goods vanishingly rare.
    """
    import math

    low_f, high_f = float(low), float(high)
    drawn = math.exp(rng.uniform(math.log(low_f), math.log(high_f)))
    # Prices end in .99 / .95 / .00 far more often than at random.
    rounded = Decimal(drawn).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    ending = rng.choices(["0.99", "0.95", "0.00", "0.49"], weights=[5, 2, 2, 1], k=1)[0]
    return (rounded + Decimal(ending)).quantize(CENTS, rounding=ROUND_HALF_UP)


def build_products(
    rng: random.Random,
    config: GenerationConfig,
    guaranteed_star_categories: tuple[str, ...] = (),
) -> list[GenProduct]:
    """Build the product catalogue with lifecycle archetypes assigned.

    `guaranteed_star_categories` names categories that must contain at least one
    STAR product. Archetypes are drawn randomly, so a category can legitimately
    end up with none — with a 5% STAR rate and ~70 products per category, that
    happens for roughly 3% of seeds. Anything that needs a reliably top-selling
    product in a specific category (the planted product-collapse anomaly does)
    would then silently have no target. Promoting one here makes the requirement
    explicit and seed-independent rather than a matter of luck.
    """
    products: list[GenProduct] = []
    archetype_keys = list(ARCHETYPE_SHARES)
    archetype_weights = [ARCHETYPE_SHARES[k] for k in archetype_keys]

    # Distribute the catalogue across categories by their declared share.
    per_category = {
        spec.name: max(6, round(config.n_products * spec.catalogue_share)) for spec in CATEGORIES
    }

    next_id = 1
    for spec in CATEGORIES:
        code = CATEGORY_CODES[spec.name]
        for seq in range(per_category[spec.name]):
            archetype = rng.choices(archetype_keys, weights=archetype_weights, k=1)[0]
            subcategory = rng.choice(spec.subcategories)
            unit_price = _log_uniform_price(rng, spec.price_low, spec.price_high)

            # Cost derives from list price and the category margin, jittered so
            # margin varies per product rather than being a category constant.
            margin = spec.margin * rng.uniform(0.82, 1.18)
            margin = min(max(margin, 0.05), 0.92)
            cost_price = (unit_price * Decimal(str(1.0 - margin))).quantize(
                SUB_CENTS, rounding=ROUND_HALF_UP
            )

            # Growth products launch inside the window; everything else predates
            # it, so most of the catalogue has full two-year history.
            if archetype is ProductArchetype.GROWTH:
                offset = rng.randint(0, int(config.total_days * 0.55))
                launch_date = config.window_start + dt.timedelta(days=offset)
            else:
                launch_date = config.window_start - dt.timedelta(days=rng.randint(30, 1500))

            # Declining lines get retired; a handful of others are discontinued.
            if archetype is ProductArchetype.DECLINING:
                is_active = rng.random() > 0.45
            else:
                is_active = rng.random() > 0.04

            name = (
                f"{rng.choice(PRODUCT_NAME_PREFIXES)} {subcategory} "
                f"{rng.choice(PRODUCT_NAME_SUFFIXES)}"
            )

            products.append(
                GenProduct(
                    id=next_id,
                    sku=f"{code}-{seq + 1:05d}",
                    name=name,
                    category=spec.name,
                    subcategory=subcategory,
                    unit_price=unit_price,
                    cost_price=cost_price,
                    launch_date=launch_date,
                    is_active=is_active,
                    archetype=archetype,
                )
            )
            next_id += 1

    return _ensure_stars(products, guaranteed_star_categories)


def _ensure_stars(products: list[GenProduct], categories: tuple[str, ...]) -> list[GenProduct]:
    """Promote a product to STAR in any named category that lacks one.

    Promotes the highest-priced STEADY product, deterministically: a STAR is
    meant to be a headline item, and price is a stable tie-break that does not
    consume randomness (which would shift every subsequent draw and change the
    whole warehouse).
    """
    for category in categories:
        in_category = [p for p in products if p.category == category]
        if any(p.archetype is ProductArchetype.STAR for p in in_category):
            continue

        candidates = [
            p for p in in_category if p.archetype is ProductArchetype.STEADY
        ] or in_category
        if not candidates:
            raise ValueError(f"category {category!r} has no products to promote")

        chosen = max(candidates, key=lambda p: (p.unit_price, -p.id))
        index = products.index(chosen)
        products[index] = dataclasses.replace(chosen, archetype=ProductArchetype.STAR)

    return products


def _signup_date(rng: random.Random, config: GenerationConfig) -> dt.date:
    """Draw a signup date, weighted towards the recent past.

    `u ** 0.62` biases the draw towards 1.0 (recent). A growing business
    acquires more customers each year, so a uniform draw would understate the
    recent cohorts and flatten acquisition trends.
    """
    span = (config.window_end - config.signup_start).days
    position = rng.random() ** 0.62
    return config.signup_start + dt.timedelta(days=int(position * span))


def build_customers(
    rng: random.Random,
    faker: Faker,
    config: GenerationConfig,
    regions: list[GenRegion],
) -> list[GenCustomer]:
    """Build the customer base.

    Segment is conditioned on acquisition channel, not drawn independently.
    That is what makes "which channel produces the most valuable customers?" a
    question with a real answer: partner-sourced customers skew Enterprise,
    social-sourced skew Consumer.
    """
    customers: list[GenCustomer] = []
    channel_keys = list(CHANNEL_PROFILES)
    channel_weights = [CHANNEL_PROFILES[k].share for k in channel_keys]
    region_weights = [region.demand_weight for region in regions]
    segment_order = (
        CustomerSegment.ENTERPRISE,
        CustomerSegment.SMB,
        CustomerSegment.CONSUMER,
    )
    base_segment_shares = [
        SEGMENT_PROFILES[segment].share_of_customers for segment in segment_order
    ]

    seen_emails: set[str] = set()

    for index in range(1, config.n_customers + 1):
        channel = rng.choices(channel_keys, weights=channel_weights, k=1)[0]
        channel_profile = CHANNEL_PROFILES[channel]

        # Segment posterior = base share x channel bias.
        segment_weights = [
            base * bias
            for base, bias in zip(base_segment_shares, channel_profile.segment_bias, strict=True)
        ]
        segment = rng.choices(segment_order, weights=segment_weights, k=1)[0]

        region = rng.choices(regions, weights=region_weights, k=1)[0]
        first_name = faker.first_name()
        last_name = faker.last_name()

        # Built rather than drawn from Faker's unique provider: deterministic,
        # guaranteed unique, and does not degrade as the pool is exhausted.
        local = f"{first_name}.{last_name}".lower().replace(" ", "").replace("'", "")
        email = f"{local}{index}@example.com"
        if email in seen_emails:  # pragma: no cover - index makes this impossible
            email = f"{local}{index}x@example.com"
        seen_emails.add(email)

        customers.append(
            GenCustomer(
                id=index,
                customer_code=f"CUS-{index:06d}",
                first_name=first_name,
                last_name=last_name,
                email=email,
                signup_date=_signup_date(rng, config),
                region_id=region.id,
                customer_segment=str(segment),
                acquisition_channel=channel,
                age_band=_weighted_choice(rng, AGE_BAND_WEIGHTS),
                is_active=rng.random() > 0.13,
                order_weight=(
                    SEGMENT_PROFILES[segment].order_weight
                    * channel_profile.order_weight
                    * region.demand_weight
                ),
            )
        )

    return customers


def build_employees(
    rng: random.Random,
    faker: Faker,
    config: GenerationConfig,
    regions: list[GenRegion],
) -> list[GenEmployee]:
    """Build the staff roster, weighted towards the larger regions."""
    employees: list[GenEmployee] = []
    region_weights = [region.demand_weight for region in regions]
    earliest_hire = config.signup_start - dt.timedelta(days=1800)
    hire_span = (config.window_end - earliest_hire).days

    for index in range(1, config.n_employees + 1):
        department = _weighted_choice(rng, DEPARTMENT_SHARES)
        role = rng.choice(DEPARTMENT_ROLES[department])
        region = rng.choices(regions, weights=region_weights, k=1)[0]
        # Headcount grows with the business, so bias hires towards recent years.
        hire_offset = int((rng.random() ** 0.7) * hire_span)

        employees.append(
            GenEmployee(
                id=index,
                employee_code=f"EMP-{index:05d}",
                name=faker.name(),
                department=department,
                role=role,
                region_id=region.id,
                hire_date=earliest_hire + dt.timedelta(days=hire_offset),
            )
        )

    return employees
