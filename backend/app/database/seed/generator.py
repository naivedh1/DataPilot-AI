"""Top-level dataset generation.

Owns the random state. One `random.Random` seeded from the config drives every
draw, and Faker is seeded from the same value, so the whole warehouse is a pure
function of `GenerationConfig`.
"""

from __future__ import annotations

import logging
import random

from faker import Faker

from app.database.seed import anomalies as anomaly_rules
from app.database.seed.config import GenerationConfig
from app.database.seed.dataset import GeneratedDataset, GenProduct, ResolvedAnomaly
from app.database.seed.entities import (
    build_customers,
    build_employees,
    build_products,
    build_regions,
)
from app.database.seed.orders import build_orders

logger = logging.getLogger(__name__)


def generate(config: GenerationConfig | None = None) -> GeneratedDataset:
    """Build the complete synthetic warehouse in memory.

    Deterministic: the same config produces byte-identical output on any
    machine. Nothing here reads the clock or the environment.
    """
    config = config or GenerationConfig()
    rng = random.Random(config.seed)
    faker = Faker("en_US")
    Faker.seed(config.seed)

    logger.info(
        "generating warehouse: seed=%s window=%s..%s customers=%s orders=%s",
        config.seed,
        config.window_start,
        config.window_end,
        config.n_customers,
        config.n_orders,
    )

    regions = build_regions()
    # The catalogue must be able to host the planted product-collapse
    # anomaly, which needs a top-selling product in a specific category.
    products = build_products(
        rng, config, guaranteed_star_categories=(anomaly_rules.PRODUCT_COLLAPSE.category,)
    )
    customers = build_customers(rng, faker, config, regions)
    employees = build_employees(rng, faker, config, regions)
    order_rows, item_rows, collapsed = build_orders(rng, config, customers, products, regions)

    dataset = GeneratedDataset(
        regions=regions,
        products=products,
        customers=customers,
        employees=employees,
        orders=order_rows,
        order_items=item_rows,
        anomalies=_resolved_anomalies(collapsed),
    )
    logger.info("generation complete: %s", dataset.counts())
    return dataset


def _resolved_anomalies(collapsed_product: GenProduct) -> list[ResolvedAnomaly]:
    """Describe the planted anomalies, including anything resolved at runtime."""
    resolved = [
        ResolvedAnomaly(
            key=anomaly.key,
            kind=str(anomaly.kind),
            description=anomaly.description,
            detail=(
                f"{anomaly.start} to {anomaly.end}, x{anomaly.multiplier}"
                + (f", target={anomaly.target}" if anomaly.target else "")
            ),
        )
        for anomaly in anomaly_rules.WINDOW_ANOMALIES
    ]

    spec = anomaly_rules.PRODUCT_COLLAPSE
    target = f"sku={collapsed_product.sku} (id={collapsed_product.id})"
    resolved.append(
        ResolvedAnomaly(
            key=spec.key,
            kind=str(spec.kind),
            description=spec.description,
            detail=f"from {spec.effective_from}, x{spec.residual_multiplier}, {target}",
        )
    )
    return resolved
