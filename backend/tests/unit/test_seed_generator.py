"""Tests for the generator: determinism, coherence and financial arithmetic.

Runs at reduced scale. The behaviour under test — reproducibility, referential
coherence, exact Decimal arithmetic — is scale-invariant, and a full 50,000-order
build in every test run would make the suite too slow to want to run.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.database.seed import anomalies as anomaly_rules
from app.database.seed.config import GenerationConfig
from app.database.seed.dataset import ORDER_COLUMNS, ORDER_ITEM_COLUMNS
from app.database.seed.generator import generate
from app.database.seed.patterns import ProductArchetype
from app.models.enums import (
    AcquisitionChannel,
    AgeBand,
    CustomerSegment,
    Department,
    OrderStatus,
    SalesChannel,
)

SMALL = GenerationConfig(n_customers=400, n_orders=2_500, n_products=120, n_employees=25)

# Column positions, resolved by name so a column reordering cannot silently
# invalidate every assertion below.
O = {name: index for index, name in enumerate(ORDER_COLUMNS)}  # noqa: E741
I = {name: index for index, name in enumerate(ORDER_ITEM_COLUMNS)}  # noqa: E741


@pytest.fixture(scope="module")
def dataset():
    return generate(SMALL)


class TestDeterminism:
    def test_same_seed_produces_identical_row_counts(self):
        assert generate(SMALL).counts() == generate(SMALL).counts()

    def test_same_seed_produces_identical_orders(self):
        assert generate(SMALL).orders == generate(SMALL).orders

    def test_same_seed_produces_identical_order_items(self):
        assert generate(SMALL).order_items == generate(SMALL).order_items

    def test_same_seed_produces_identical_customers(self):
        first = [c.to_row() for c in generate(SMALL).customers]
        second = [c.to_row() for c in generate(SMALL).customers]
        assert first == second

    def test_different_seed_produces_different_data(self):
        """Reproducibility must come from the seed, not from the code being
        insufficiently random."""
        other = generate(
            GenerationConfig(
                seed=99, n_customers=400, n_orders=2_500, n_products=120, n_employees=25
            )
        )
        assert other.orders != generate(SMALL).orders

    def test_generation_does_not_depend_on_the_clock(self, dataset):
        """Every date must fall inside the pinned window, regardless of today."""
        dates = [row[O["order_date"]] for row in dataset.orders]
        assert min(dates) >= SMALL.window_start
        assert max(dates) <= SMALL.window_end


class TestScale:
    def test_requested_order_count_is_exact(self, dataset):
        assert len(dataset.orders) == SMALL.n_orders

    def test_requested_customer_count_is_exact(self, dataset):
        assert len(dataset.customers) == SMALL.n_customers

    def test_every_order_has_at_least_one_line(self, dataset):
        order_ids = {row[O["id"]] for row in dataset.orders}
        with_lines = {row[I["order_id"]] for row in dataset.order_items}
        assert order_ids == with_lines

    def test_average_basket_is_plausible(self, dataset):
        average = len(dataset.order_items) / len(dataset.orders)
        assert 1.5 < average < 5.0, f"implausible basket size: {average:.2f}"

    def test_all_twelve_regions_exist(self, dataset):
        assert len(dataset.regions) == 12


class TestFinancialArithmetic:
    """The database enforces these identities as CHECK constraints, so a
    violation here means seeding would fail outright. Catching it in a unit test
    localises the fault to the generator instead."""

    def test_line_total_identity_holds_exactly(self, dataset):
        for row in dataset.order_items:
            expected = row[I["quantity"]] * row[I["unit_price"]] - row[I["discount_amount"]]
            assert row[I["line_total"]] == expected

    def test_order_total_identity_holds_exactly(self, dataset):
        for row in dataset.orders:
            expected = (
                row[O["subtotal"]]
                - row[O["discount_amount"]]
                + row[O["tax_amount"]]
                + row[O["shipping_amount"]]
            )
            assert row[O["total_amount"]] == expected

    def test_subtotal_equals_the_sum_of_its_lines(self, dataset):
        sums: dict[int, Decimal] = {}
        for row in dataset.order_items:
            key = row[I["order_id"]]
            sums[key] = sums.get(key, Decimal("0.00")) + row[I["line_total"]]
        for row in dataset.orders:
            assert row[O["subtotal"]] == sums[row[O["id"]]]

    def test_every_monetary_value_is_decimal_not_float(self, dataset):
        """Float money would drift on summation and break the identities above."""
        for name in (
            "subtotal",
            "discount_amount",
            "tax_amount",
            "shipping_amount",
            "total_amount",
        ):
            assert isinstance(dataset.orders[0][O[name]], Decimal), name
        for name in ("unit_price", "discount_amount", "line_total"):
            assert isinstance(dataset.order_items[0][I[name]], Decimal), name

    def test_money_carries_at_most_two_decimal_places(self, dataset):
        for row in dataset.orders[:500]:
            assert -row[O["total_amount"]].as_tuple().exponent <= 2

    def test_no_negative_amounts(self, dataset):
        for row in dataset.orders:
            for name in (
                "subtotal",
                "discount_amount",
                "tax_amount",
                "shipping_amount",
                "total_amount",
            ):
                assert row[O[name]] >= 0, name

    def test_discount_never_exceeds_subtotal(self, dataset):
        for row in dataset.orders:
            assert row[O["discount_amount"]] <= row[O["subtotal"]]

    def test_quantities_are_positive(self, dataset):
        assert all(row[I["quantity"]] > 0 for row in dataset.order_items)


class TestReferentialCoherence:
    def test_order_customer_ids_all_exist(self, dataset):
        ids = {c.id for c in dataset.customers}
        assert all(row[O["customer_id"]] in ids for row in dataset.orders)

    def test_order_item_product_ids_all_exist(self, dataset):
        ids = {p.id for p in dataset.products}
        assert all(row[I["product_id"]] in ids for row in dataset.order_items)

    def test_shipping_regions_all_exist(self, dataset):
        ids = {r.id for r in dataset.regions}
        assert all(row[O["shipping_region_id"]] in ids for row in dataset.orders)

    def test_no_order_predates_its_customers_signup(self, dataset):
        signup = {c.id: c.signup_date for c in dataset.customers}
        for row in dataset.orders:
            assert row[O["order_date"]] >= signup[row[O["customer_id"]]]

    def test_no_product_is_sold_before_it_launches(self, dataset):
        """Regression guard.

        Product weights are cached per month and evaluate eligibility at the
        month's midpoint, so a product launching on the 20th was briefly
        purchasable on the 3rd. The fix re-checks the launch date against the
        actual order date at selection time.
        """
        launch = {p.id: p.launch_date for p in dataset.products}
        order_date = {row[O["id"]]: row[O["order_date"]] for row in dataset.orders}
        for row in dataset.order_items:
            assert order_date[row[I["order_id"]]] >= launch[row[I["product_id"]]]

    def test_order_numbers_are_unique(self, dataset):
        numbers = [row[O["order_number"]] for row in dataset.orders]
        assert len(numbers) == len(set(numbers))

    def test_customer_emails_are_unique(self, dataset):
        emails = [c.email for c in dataset.customers]
        assert len(emails) == len(set(emails))

    def test_skus_are_unique(self, dataset):
        skus = [p.sku for p in dataset.products]
        assert len(skus) == len(set(skus))


class TestControlledVocabularies:
    """Values must match the CHECK constraints, or the load fails."""

    def test_order_statuses_are_valid(self, dataset):
        allowed = {s.value for s in OrderStatus}
        assert {row[O["status"]] for row in dataset.orders} <= allowed

    def test_sales_channels_are_valid(self, dataset):
        allowed = {c.value for c in SalesChannel}
        assert {row[O["sales_channel"]] for row in dataset.orders} <= allowed

    def test_customer_segments_are_valid(self, dataset):
        allowed = {s.value for s in CustomerSegment}
        assert {c.customer_segment for c in dataset.customers} <= allowed

    def test_acquisition_channels_are_valid(self, dataset):
        allowed = {c.value for c in AcquisitionChannel}
        assert {c.acquisition_channel for c in dataset.customers} <= allowed

    def test_age_bands_are_valid(self, dataset):
        allowed = {b.value for b in AgeBand}
        assert {c.age_band for c in dataset.customers} <= allowed

    def test_departments_are_valid(self, dataset):
        allowed = {d.value for d in Department}
        assert {e.department for e in dataset.employees} <= allowed

    def test_all_statuses_actually_occur(self, dataset):
        """A status that never appears makes status analysis meaningless."""
        assert {row[O["status"]] for row in dataset.orders} == {s.value for s in OrderStatus}


class TestBusinessPatterns:
    def test_enterprise_orders_are_larger_than_consumer_orders(self, dataset):
        segment = {c.id: c.customer_segment for c in dataset.customers}
        totals: dict[str, list[Decimal]] = {}
        for row in dataset.orders:
            totals.setdefault(segment[row[O["customer_id"]]], []).append(row[O["total_amount"]])
        enterprise = sum(totals["Enterprise"]) / len(totals["Enterprise"])
        consumer = sum(totals["Consumer"]) / len(totals["Consumer"])
        assert enterprise > consumer * 5

    def test_average_order_values_are_commercially_plausible(self, dataset):
        """Guards the bug where consumers bought industrial equipment and
        consumer AOV came out above 500."""
        segment = {c.id: c.customer_segment for c in dataset.customers}
        totals: dict[str, list[Decimal]] = {}
        for row in dataset.orders:
            totals.setdefault(segment[row[O["customer_id"]]], []).append(row[O["total_amount"]])
        consumer_aov = sum(totals["Consumer"]) / len(totals["Consumer"])
        assert Decimal("80") < consumer_aov < Decimal("450"), consumer_aov

    def test_monthly_order_volume_varies(self, dataset):
        by_month: dict[tuple[int, int], int] = {}
        for row in dataset.orders:
            day: dt.date = row[O["order_date"]]
            key = (day.year, day.month)
            by_month[key] = by_month.get(key, 0) + 1
        counts = list(by_month.values())
        assert max(counts) > min(counts) * 1.4

    def test_the_window_is_fully_covered(self, dataset):
        months = {(row[O["order_date"]].year, row[O["order_date"]].month) for row in dataset.orders}
        assert len(months) == 24

    def test_every_region_receives_orders(self, dataset):
        assert len({row[O["shipping_region_id"]] for row in dataset.orders}) == 12

    def test_catalogue_contains_every_archetype(self, dataset):
        assert {p.archetype for p in dataset.products} == set(ProductArchetype)


class TestAnomalyPlanting:
    def test_all_five_anomalies_are_reported(self, dataset):
        assert {a.key for a in dataset.anomalies} == set(anomaly_rules.ALL_ANOMALY_KEYS)

    def test_the_product_collapse_target_is_resolved(self, dataset):
        """Regression guard.

        The target was previously found by filtering for a STAR product in a
        given category. Archetypes are drawn randomly, so for some seeds no such
        product existed and the anomaly silently had no target — leaving the
        evaluation suite asserting against data that was never generated.
        """
        collapse = next(a for a in dataset.anomalies if a.key == anomaly_rules.PRODUCT_COLLAPSE.key)
        assert "sku=" in collapse.detail
        assert "unresolved" not in collapse.detail

    def test_the_guaranteed_star_category_contains_a_star(self, dataset):
        category = anomaly_rules.PRODUCT_COLLAPSE.category
        stars = [
            p
            for p in dataset.products
            if p.category == category and p.archetype is ProductArchetype.STAR
        ]
        assert stars, f"no STAR product in {category}"

    def test_outage_week_is_quieter_than_its_neighbours(self, dataset):
        spec = anomaly_rules.PLATFORM_OUTAGE
        inside = sum(1 for row in dataset.orders if spec.covers(row[O["order_date"]]))
        before_start = spec.start - dt.timedelta(days=7)
        before = sum(
            1 for row in dataset.orders if before_start <= row[O["order_date"]] < spec.start
        )
        assert inside < before * 0.6, f"outage not visible: {inside} vs {before}"

    def test_flash_sale_week_is_busier_than_its_neighbours(self, dataset):
        spec = anomaly_rules.FLASH_SALE
        inside = sum(1 for row in dataset.orders if spec.covers(row[O["order_date"]]))
        before_start = spec.start - dt.timedelta(days=7)
        before = sum(
            1 for row in dataset.orders if before_start <= row[O["order_date"]] < spec.start
        )
        assert inside > before * 1.4, f"flash sale not visible: {inside} vs {before}"
