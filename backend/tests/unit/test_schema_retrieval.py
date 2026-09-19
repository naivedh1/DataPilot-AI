"""Tests for schema intelligence.

Retrieval is deterministic by design — no embeddings, no model call — so it can
be tested exactly rather than statistically. That determinism is the point: the
evaluation suite measures SQL generation, and a retriever that returned
different schema run to run would make those numbers meaningless.
"""

from __future__ import annotations

import pytest

from app.services.schema import (
    METRICS,
    get_schema,
    match_metrics,
    render_full_schema,
    retrieve,
    score_tables,
    stale_catalogue_entries,
    tokenize,
    undocumented_columns,
)

ALL_TABLES = {"orders", "order_items", "customers", "products", "regions", "employees"}


class TestCatalogueIntegrity:
    def test_every_column_is_documented(self):
        """An undocumented column is one the model will ignore or guess at."""
        assert undocumented_columns() == []

    def test_no_catalogue_entry_is_stale(self):
        """Documentation that outlived its column actively misleads the model."""
        assert stale_catalogue_entries() == []

    def test_all_six_tables_are_present(self):
        assert set(get_schema()) == ALL_TABLES

    def test_metric_names_are_unique(self):
        names = [metric.name for metric in METRICS]
        assert len(names) == len(set(names))

    def test_every_metric_names_the_tables_it_needs(self):
        for metric in METRICS:
            assert metric.required_tables, metric.name
            for table in metric.required_tables:
                assert table in ALL_TABLES, f"{metric.name} -> {table}"

    def test_metric_expressions_are_sql_not_prose(self):
        """A prose 'definition' is exactly what lets the model invent its own."""
        for metric in METRICS:
            assert any(
                token in metric.expression.upper()
                for token in ("SUM(", "COUNT(", "AVG(", "MAX(", "MIN(")
            ), metric.name


class TestIntrospection:
    def test_primary_keys_are_identified(self):
        orders = get_schema()["orders"]
        assert orders.column("id").is_primary_key

    def test_foreign_keys_are_resolved_to_their_target(self):
        orders = get_schema()["orders"]
        assert orders.column("customer_id").foreign_key == "customers.id"
        assert orders.column("shipping_region_id").foreign_key == "regions.id"

    def test_check_constraint_values_are_extracted(self):
        """Showing the model the exact literals is the most effective way to
        stop it inventing 'ENTERPRISE' when the data says 'Enterprise'."""
        status = get_schema()["orders"].column("status")
        assert set(status.allowed_values) == {"completed", "pending", "cancelled", "returned"}

    def test_segment_values_are_extracted(self):
        segment = get_schema()["customers"].column("customer_segment")
        assert set(segment.allowed_values) == {"Enterprise", "SMB", "Consumer"}

    def test_columns_without_a_check_constraint_have_no_allowed_values(self):
        assert get_schema()["orders"].column("total_amount").allowed_values == ()

    def test_rendered_table_includes_types_and_meaning(self):
        rendered = get_schema()["orders"].render()
        assert "TABLE orders" in rendered
        assert "order_date" in rendered
        assert "NUMERIC" in rendered.upper()
        assert "Allowed values:" in rendered

    def test_rendered_schema_warns_about_the_status_filter(self):
        """The single most consequential piece of domain knowledge here."""
        rendered = get_schema()["orders"].render()
        assert "completed" in rendered


class TestTokenizer:
    def test_stopwords_are_removed(self):
        assert "the" not in tokenize("what is the revenue")

    def test_plurals_are_normalised(self):
        tokens = tokenize("show me all products")
        assert "product" in tokens

    def test_y_plurals_are_normalised(self):
        assert "category" in tokenize("revenue by categories")

    def test_case_is_ignored(self):
        assert tokenize("REVENUE") == tokenize("revenue")

    def test_short_noise_tokens_are_dropped(self):
        assert "a" not in tokenize("a b revenue")


class TestRelevanceScoring:
    def test_an_explicit_table_name_scores_highest(self):
        scores = score_tables("how many employees do we have")
        assert scores["employees"] == max(scores.values())

    def test_a_data_literal_is_a_strong_signal(self):
        """'Enterprise' can only have come from customers.customer_segment."""
        scores = score_tables("how many Enterprise accounts are there")
        assert scores["customers"] > scores["products"]

    def test_unrelated_tables_score_low(self):
        scores = score_tables("how many employees per department")
        assert scores["order_items"] < 2.0

    def test_department_does_not_pull_in_products(self):
        """Regression guard.

        'department' was listed as a synonym for products.category, so every
        staffing question retrieved the products table.
        """
        assert retrieve("how many employees work in each department").table_names == ("employees",)


class TestMetricMatching:
    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("what was our revenue last year", "revenue"),
            ("show me the average order value by segment", "average_order_value"),
            ("what is the return rate by category", "return_rate"),
            ("gross margin by product", "gross_margin"),
            ("how many units did we sell", "units_sold"),
            ("repeat purchase rate", "repeat_rate"),
        ],
    )
    def test_metric_is_matched_from_natural_phrasing(self, question, expected):
        assert expected in {metric.name for metric in match_metrics(question)}

    def test_multiword_metric_phrases_are_matched(self):
        """Token matching alone cannot see 'average order value' as one thing."""
        assert "average_order_value" in {
            metric.name for metric in match_metrics("what is the average order value")
        }

    def test_no_metric_matches_a_structural_question(self):
        assert match_metrics("which regions exist") == ()


class TestRetrieval:
    def test_retrieval_is_deterministic(self):
        """The evaluation suite measures the model, not the retriever's mood."""
        question = "monthly revenue by region for the last 12 months"
        first = retrieve(question)
        second = retrieve(question)
        assert first.table_names == second.table_names
        assert [m.name for m in first.metrics] == [m.name for m in second.metrics]

    def test_does_not_return_the_whole_warehouse(self):
        """The entire point: relevant schema, not all schema."""
        result = retrieve("how many employees are in each department")
        assert len(result.tables) < len(ALL_TABLES)

    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("top 10 products by revenue this year", {"products", "order_items", "orders"}),
            ("average order value by customer segment", {"orders", "customers"}),
            ("monthly revenue by region", {"orders", "regions"}),
            ("return rate by product category", {"products", "orders"}),
            ("how many employees per department", {"employees"}),
        ],
    )
    def test_expected_tables_are_retrieved(self, question, expected):
        assert expected <= set(retrieve(question).table_names)

    def test_order_items_always_brings_orders(self):
        """order_items is only reachable through orders."""
        result = retrieve("how many units were sold")
        if "order_items" in result.table_names:
            assert "orders" in result.table_names

    def test_metric_required_tables_are_always_included(self):
        """'margin' needs products even though the question never says 'product'."""
        result = retrieve("what is our gross margin")
        assert {"products", "order_items", "orders"} <= set(result.table_names)

    def test_unmatched_question_falls_back_to_the_core(self):
        """Better than returning nothing, and better than returning everything."""
        result = retrieve("hello there")
        assert set(result.table_names) == {"orders", "customers"}

    def test_max_tables_is_respected_for_ordinary_questions(self):
        result = retrieve("monthly revenue", max_tables=2)
        assert len(result.tables) <= 3  # +1 for an order_items/orders dependency

    def test_extra_tables_are_forced_in(self):
        """Used by follow-up turns, where the previous query's tables stay
        relevant even if this turn's wording no longer mentions them."""
        result = retrieve("only the last 6 months", extra_tables=("regions",))
        assert "regions" in result.table_names

    def test_join_paths_are_provided_for_selected_tables(self):
        result = retrieve("revenue by region")
        assert any("shipping_region_id" in clause for clause in result.joins)

    def test_no_join_is_offered_between_unrelated_tables(self):
        result = retrieve("how many employees per department")
        assert result.joins == () or all("order" not in j for j in result.joins)


class TestRendering:
    def test_rendered_output_contains_metric_definitions(self):
        rendered = retrieve("what was our revenue by region").render()
        assert "METRIC DEFINITIONS" in rendered
        assert "status = 'completed'" in rendered

    def test_rendered_output_contains_join_paths(self):
        rendered = retrieve("revenue by region").render()
        assert "JOIN PATHS" in rendered

    def test_rendered_output_omits_irrelevant_tables(self):
        rendered = retrieve("how many employees per department").render()
        assert "TABLE order_items" not in rendered

    def test_retrieved_schema_is_materially_smaller_than_the_full_schema(self):
        """If retrieval saved nothing, it would be ceremony rather than design."""
        focused = len(retrieve("how many employees per department").render())
        everything = len(render_full_schema())
        assert focused < everything * 0.5

    def test_full_schema_renders_every_table(self):
        rendered = render_full_schema()
        for table in ALL_TABLES:
            assert f"TABLE {table}" in rendered

    def test_caveats_are_surfaced_to_the_model(self):
        """The product-revenue fan-out trap is the most common way to get a
        plausible but wrong number out of this schema."""
        rendered = retrieve("revenue by product category").render()
        assert "CAVEAT" in rendered
