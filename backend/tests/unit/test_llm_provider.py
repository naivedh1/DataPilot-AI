"""Provider-selection and error-translation tests.

No network. The live Gemini path is exercised by `tests/integration/test_gemini_live.py`,
which is marked `llm` and skipped unless a key is configured.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, SecretStr

from app.core.config import Settings
from app.schemas.agent import PlannerDecision, SQLGeneration
from app.services.llm import get_provider, reset_provider
from app.services.llm.offline import OfflineProvider, build_sql


class TestProviderSelection:
    def setup_method(self) -> None:
        reset_provider()

    def teardown_method(self) -> None:
        reset_provider()

    def test_offline_baseline_is_used_without_a_key(self):
        """A missing key must degrade the system, not break it."""
        provider = get_provider(Settings(gemini_api_key=SecretStr("")))
        assert isinstance(provider, OfflineProvider)
        assert provider.is_live is False

    def test_the_env_example_placeholder_is_not_treated_as_a_key(self):
        reset_provider()
        provider = get_provider(Settings(gemini_api_key=SecretStr("your-gemini-api-key-here")))
        assert isinstance(provider, OfflineProvider)


class TestOfflineBaselineHonesty:
    """The baseline must never answer what it cannot answer."""

    def test_every_response_is_flagged_as_simulated(self):
        provider = OfflineProvider()
        _, usage = provider.complete_json(
            system="s", prompt="QUESTION: revenue by region", schema=SQLGeneration
        )
        assert usage.simulated is True

    @pytest.mark.parametrize(
        "question",
        [
            "What is the meaning of life?",
            "Tell me a joke",
            "What is our employee satisfaction score?",
            "Which sales rep closed the most deals?",
            "How much did we spend on advertising?",
        ],
    )
    def test_questions_outside_the_warehouse_are_declined(self, question):
        """Returning a query that merely runs would give a confident wrong
        answer, which is worse than no answer."""
        assert build_sql(question) is None

    @pytest.mark.parametrize(
        "question",
        [
            "revenue by region",
            "monthly revenue",
            "average order value by segment",
            "return rate by category",
            "how many employees per department",
        ],
    )
    def test_answerable_questions_produce_sql(self, question):
        generated = build_sql(question)
        assert generated is not None
        assert generated.sql.upper().startswith("SELECT")

    def test_generated_sql_is_never_a_write(self):
        for question in ("revenue by region", "monthly orders", "top 10 products"):
            generated = build_sql(question)
            assert generated is not None
            upper = generated.sql.upper()
            for keyword in ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE"):
                assert keyword not in upper

    def test_product_revenue_avoids_the_fan_out_trap(self):
        """Regression guard.

        Joining order_items produces one row per line, so SUM(o.total_amount)
        counts each order's total once per line it contains. Line-level revenue
        is the only correct measure at that grain.
        """
        generated = build_sql("top 10 products by revenue")
        assert generated is not None
        assert "oi.line_total" in generated.sql
        assert "SUM(o.total_amount)" not in generated.sql

    def test_a_plain_customer_count_does_not_join_through_orders(self):
        """Regression guard: counting customers via orders silently answers the
        narrower question 'customers who have ordered'."""
        generated = build_sql("how many customers do we have?")
        assert generated is not None
        assert "JOIN orders" not in generated.sql
        assert "FROM customers" in generated.sql

    def test_grouping_by_status_does_not_also_filter_on_status(self):
        """Regression guard: it collapsed the result to a single row."""
        generated = build_sql("order counts by status")
        assert generated is not None
        assert "GROUP BY o.status" in generated.sql
        assert "o.status = 'completed'" not in generated.sql

    def test_an_unknown_schema_type_raises_rather_than_inventing_one(self):
        class Unknown(BaseModel):
            value: str

        with pytest.raises(NotImplementedError):
            OfflineProvider().complete_json(system="s", prompt="QUESTION: x", schema=Unknown)


class TestFollowUpResolution:
    def test_a_refinement_inherits_the_previous_question(self):
        provider = OfflineProvider()
        prompt = (
            "QUESTION: only the last 6 months\n\n"
            "CONVERSATION SO FAR:\n"
            "   Q: Show me monthly revenue by region\n"
        )
        plan, _ = provider.complete_json(system="s", prompt=prompt, schema=PlannerDecision)
        assert "revenue" in plan.resolved_question.lower()
        assert "region" in plan.resolved_question.lower()

    def test_a_standalone_question_is_not_merged(self):
        provider = OfflineProvider()
        prompt = (
            "QUESTION: What is the return rate by product category?\n\n"
            "CONVERSATION SO FAR:\n"
            "   Q: Show me monthly revenue by region\n"
        )
        plan, _ = provider.complete_json(system="s", prompt=prompt, schema=PlannerDecision)
        assert "region" not in plan.resolved_question.lower()


class TestConfiguration:
    def test_the_default_model_is_not_a_retired_one(self):
        """The 2.5 family returns 404 for keys created after its retirement, so
        a stale default silently breaks every fresh install."""
        assert not Settings().gemini_model.startswith("gemini-2.")

    def test_the_default_model_is_pinned_not_an_alias(self):
        """An alias such as `gemini-flash-latest` can change underneath the
        evaluation suite between runs, making results incomparable."""
        assert "latest" not in Settings().gemini_model

    def test_temperature_defaults_to_deterministic(self):
        """This is SQL, not prose."""
        assert Settings().llm_temperature == 0.0
