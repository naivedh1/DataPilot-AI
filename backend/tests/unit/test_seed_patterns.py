"""Tests for the behavioural model behind the synthetic data.

These assert the *claims the warehouse makes about itself*. If November stops
being the strongest month, or Enterprise stops being the up-market segment,
every analytical expectation downstream silently changes meaning.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.database.seed import anomalies as anomaly_rules
from app.database.seed.patterns import (
    CATEGORY_REFUND_BIAS,
    CATEGORY_SEASONALITY,
    CHANNEL_PROFILES,
    MONTH_SEASONALITY,
    SEGMENT_CATEGORY_AFFINITY,
    SEGMENT_PROFILES,
    WEEKDAY_SEASONALITY,
    ProductArchetype,
    base_intensity,
    category_multiplier,
    lifecycle_multiplier,
    month_multiplier,
    segment_category_affinity,
    trend_multiplier,
    weekday_multiplier,
)
from app.models.enums import CustomerSegment


class TestTrend:
    def test_growth_compounds_over_a_year(self):
        assert trend_multiplier(0, 0.22) == pytest.approx(1.0)
        assert trend_multiplier(365, 0.22) == pytest.approx(1.22, abs=0.01)

    def test_growth_compounds_over_two_years(self):
        assert trend_multiplier(730, 0.22) == pytest.approx(1.22**2, abs=0.01)

    def test_trend_is_monotonically_increasing(self):
        values = [trend_multiplier(day, 0.22) for day in range(0, 730, 30)]
        assert values == sorted(values)

    def test_zero_growth_is_flat(self):
        assert trend_multiplier(500, 0.0) == pytest.approx(1.0)

    def test_negative_day_index_is_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            trend_multiplier(-1, 0.22)


class TestSeasonality:
    def test_twelve_monthly_multipliers(self):
        assert len(MONTH_SEASONALITY) == 12

    def test_seven_weekday_multipliers(self):
        assert len(WEEKDAY_SEASONALITY) == 7

    def test_november_is_the_annual_peak(self):
        assert month_multiplier(dt.date(2025, 11, 10)) == max(MONTH_SEASONALITY)

    def test_february_is_the_annual_floor(self):
        assert month_multiplier(dt.date(2025, 2, 10)) == min(MONTH_SEASONALITY)

    def test_seasonality_is_not_flat(self):
        """A flat curve would make every month-over-month question meaningless."""
        assert max(MONTH_SEASONALITY) / min(MONTH_SEASONALITY) > 1.5

    def test_weekends_are_quieter_than_weekdays(self):
        saturday = weekday_multiplier(dt.date(2025, 11, 8))
        sunday = weekday_multiplier(dt.date(2025, 11, 9))
        tuesday = weekday_multiplier(dt.date(2025, 11, 11))
        assert saturday < tuesday
        assert sunday < tuesday

    def test_base_intensity_combines_all_three_factors(self):
        day = dt.date(2025, 11, 11)
        expected = trend_multiplier(100, 0.22) * month_multiplier(day) * weekday_multiplier(day)
        assert base_intensity(day, 100, 0.22) == pytest.approx(expected)


class TestCategorySeasonality:
    def test_every_category_has_twelve_months(self):
        for category, curve in CATEGORY_SEASONALITY.items():
            assert len(curve) == 12, category

    def test_garden_peaks_in_summer_not_winter(self):
        assert category_multiplier("Outdoor & Garden", 6) > category_multiplier(
            "Outdoor & Garden", 12
        )

    def test_fitness_peaks_in_january(self):
        january = category_multiplier("Health & Fitness", 1)
        assert january == max(CATEGORY_SEASONALITY["Health & Fitness"])

    def test_electronics_peaks_in_q4(self):
        assert category_multiplier("Consumer Electronics", 11) > category_multiplier(
            "Consumer Electronics", 4
        )

    def test_categories_peak_in_different_months(self):
        """If every category peaked together, category seasonality would be a
        scaled copy of the overall curve and carry no extra information."""
        peaks = {
            category: curve.index(max(curve)) for category, curve in CATEGORY_SEASONALITY.items()
        }
        assert len(set(peaks.values())) >= 3

    def test_unknown_category_is_neutral(self):
        assert category_multiplier("Nonexistent", 6) == 1.0

    @pytest.mark.parametrize("month", [0, 13, -1])
    def test_invalid_month_is_rejected(self, month):
        with pytest.raises(ValueError, match=r"1\.\.12"):
            category_multiplier("Apparel", month)


class TestSegmentProfiles:
    def test_all_three_segments_are_profiled(self):
        assert set(SEGMENT_PROFILES) == set(CustomerSegment)

    def test_customer_shares_sum_to_one(self):
        total = sum(p.share_of_customers for p in SEGMENT_PROFILES.values())
        assert total == pytest.approx(1.0)

    def test_consumers_are_the_largest_segment_by_headcount(self):
        shares = {s: p.share_of_customers for s, p in SEGMENT_PROFILES.items()}
        assert max(shares, key=lambda k: shares[k]) == CustomerSegment.CONSUMER

    def test_enterprise_orders_less_often_than_consumers(self):
        assert (
            SEGMENT_PROFILES[CustomerSegment.ENTERPRISE].order_weight
            < SEGMENT_PROFILES[CustomerSegment.CONSUMER].order_weight
        )

    def test_enterprise_baskets_are_larger_than_consumer_baskets(self):
        enterprise = SEGMENT_PROFILES[CustomerSegment.ENTERPRISE]
        consumer = SEGMENT_PROFILES[CustomerSegment.CONSUMER]
        assert enterprise.basket_min > consumer.basket_min
        assert enterprise.basket_max > consumer.basket_max
        assert enterprise.quantity_max > consumer.quantity_max

    def test_price_affinity_orders_the_segments(self):
        """Enterprise buys up-market, consumers down-market. This is what makes
        average order value differ by segment rather than being hard-coded."""
        assert (
            SEGMENT_PROFILES[CustomerSegment.CONSUMER].price_affinity
            < SEGMENT_PROFILES[CustomerSegment.SMB].price_affinity
            < SEGMENT_PROFILES[CustomerSegment.ENTERPRISE].price_affinity
        )

    def test_basket_ranges_are_well_formed(self):
        for segment, profile in SEGMENT_PROFILES.items():
            assert profile.basket_min <= profile.basket_max, segment
            assert profile.quantity_min <= profile.quantity_max, segment
            assert profile.basket_min >= 1, segment
            assert profile.quantity_min >= 1, segment

    def test_discount_rates_are_probabilities(self):
        for segment, profile in SEGMENT_PROFILES.items():
            assert 0.0 <= profile.line_discount_rate <= 1.0, segment
            assert 0.0 <= profile.order_discount_rate <= 1.0, segment


class TestSegmentCategoryAffinity:
    def test_every_segment_has_an_affinity_map(self):
        assert set(SEGMENT_CATEGORY_AFFINITY) == set(CustomerSegment)

    def test_all_segments_cover_all_categories(self):
        categories = set(CATEGORY_SEASONALITY)
        for segment, affinities in SEGMENT_CATEGORY_AFFINITY.items():
            assert set(affinities) == categories, segment

    def test_consumers_rarely_buy_industrial_equipment(self):
        """The bug this prevents: without segment/category fit, consumer baskets
        filled with multi-thousand-pound machinery and consumer AOV came out at
        several hundred rather than a couple of hundred."""
        consumer = segment_category_affinity(CustomerSegment.CONSUMER, "Industrial Equipment")
        enterprise = segment_category_affinity(CustomerSegment.ENTERPRISE, "Industrial Equipment")
        assert consumer < 0.2
        assert enterprise > consumer * 10

    def test_enterprise_rarely_buys_apparel(self):
        assert segment_category_affinity(
            CustomerSegment.ENTERPRISE, "Apparel"
        ) < segment_category_affinity(CustomerSegment.CONSUMER, "Apparel")

    def test_unknown_pairing_is_neutral(self):
        assert segment_category_affinity("Nonexistent", "Apparel") == 1.0
        assert segment_category_affinity(CustomerSegment.SMB, "Nonexistent") == 1.0


class TestChannelProfiles:
    def test_channel_shares_sum_to_one(self):
        assert sum(p.share for p in CHANNEL_PROFILES.values()) == pytest.approx(1.0)

    def test_referral_produces_better_customers_than_social(self):
        assert CHANNEL_PROFILES["Referral"].order_weight > CHANNEL_PROFILES["Social"].order_weight

    def test_partner_channel_skews_enterprise(self):
        """segment_bias is (Enterprise, SMB, Consumer)."""
        partner = CHANNEL_PROFILES["Partner"].segment_bias
        social = CHANNEL_PROFILES["Social"].segment_bias
        assert partner[0] > social[0]
        assert social[2] > partner[2]

    def test_every_channel_has_three_segment_biases(self):
        for channel, profile in CHANNEL_PROFILES.items():
            assert len(profile.segment_bias) == 3, channel


class TestProductLifecycle:
    def test_declining_products_lose_ground(self):
        start = lifecycle_multiplier(ProductArchetype.DECLINING, 0.0)
        end = lifecycle_multiplier(ProductArchetype.DECLINING, 1.0)
        assert end < start * 0.4

    def test_growth_products_gain_ground(self):
        start = lifecycle_multiplier(ProductArchetype.GROWTH, 0.0)
        end = lifecycle_multiplier(ProductArchetype.GROWTH, 1.0)
        assert end > start * 3

    def test_steady_products_do_not_move(self):
        assert lifecycle_multiplier(ProductArchetype.STEADY, 0.0) == lifecycle_multiplier(
            ProductArchetype.STEADY, 1.0
        )

    def test_stars_outsell_ordinary_products(self):
        assert lifecycle_multiplier(ProductArchetype.STAR, 0.5) > lifecycle_multiplier(
            ProductArchetype.STEADY, 0.5
        )

    def test_multipliers_are_always_positive(self):
        for archetype in ProductArchetype:
            for progress in (0.0, 0.25, 0.5, 0.75, 1.0):
                assert lifecycle_multiplier(archetype, progress) > 0

    @pytest.mark.parametrize("progress", [-0.01, 1.01, 2.0])
    def test_progress_outside_the_window_is_rejected(self, progress):
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            lifecycle_multiplier(ProductArchetype.STEADY, progress)


class TestReturnBias:
    def test_apparel_returns_more_than_industrial(self):
        assert CATEGORY_REFUND_BIAS["Apparel"] > CATEGORY_REFUND_BIAS["Industrial Equipment"]

    def test_every_category_has_a_return_bias(self):
        assert set(CATEGORY_REFUND_BIAS) == set(CATEGORY_SEASONALITY)

    def test_biases_are_positive(self):
        assert all(bias > 0 for bias in CATEGORY_REFUND_BIAS.values())


class TestAnomalyRules:
    def test_volume_multiplier_is_neutral_outside_any_window(self):
        assert anomaly_rules.volume_multiplier(dt.date(2025, 5, 14)) == 1.0

    def test_flash_sale_raises_volume(self):
        inside = anomaly_rules.volume_multiplier(dt.date(2026, 3, 18))
        assert inside > 1.5

    def test_outage_suppresses_volume(self):
        inside = anomaly_rules.volume_multiplier(dt.date(2026, 2, 11))
        assert inside < 0.5

    def test_window_boundaries_are_inclusive(self):
        spec = anomaly_rules.PLATFORM_OUTAGE
        assert anomaly_rules.volume_multiplier(spec.start) < 1.0
        assert anomaly_rules.volume_multiplier(spec.end) < 1.0
        assert anomaly_rules.volume_multiplier(spec.end + dt.timedelta(days=1)) == 1.0

    def test_regional_anomaly_affects_only_its_target(self):
        day = dt.date(2025, 10, 1)
        assert anomaly_rules.region_multiplier("Nordics", day) < 1.0
        assert anomaly_rules.region_multiplier("DACH", day) == 1.0

    def test_regional_anomaly_is_time_bounded(self):
        assert anomaly_rules.region_multiplier("Nordics", dt.date(2026, 5, 1)) == 1.0

    def test_return_anomaly_affects_only_its_target(self):
        day = dt.date(2026, 2, 1)
        assert anomaly_rules.return_rate_multiplier("Apparel", day) > 2.0
        assert anomaly_rules.return_rate_multiplier("Home & Kitchen", day) == 1.0

    def test_anomaly_keys_are_unique(self):
        keys = list(anomaly_rules.ALL_ANOMALY_KEYS)
        assert len(keys) == len(set(keys))

    def test_five_anomalies_are_planted(self):
        assert len(anomaly_rules.ALL_ANOMALY_KEYS) == 5

    def test_every_anomaly_documents_itself(self):
        """Descriptions are the ground truth Phase 13 evaluates against."""
        for anomaly in anomaly_rules.WINDOW_ANOMALIES:
            assert len(anomaly.description) > 60, anomaly.key
        assert len(anomaly_rules.PRODUCT_COLLAPSE.description) > 60
