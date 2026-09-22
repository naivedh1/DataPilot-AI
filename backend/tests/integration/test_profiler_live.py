"""The profiler against the real warehouse.

The unit tests cover the finding logic on synthetic profiles. What can only be
tested here is that the generated SQL actually parses and runs against
PostgreSQL, and that what it measures matches what the warehouse holds.
"""

from __future__ import annotations

import pytest

from app.database.executor import execute_readonly
from app.services.profiler import profile_table, profile_warehouse
from app.services.schema.introspect import get_schema

pytestmark = pytest.mark.integration


class TestProfilingTheWarehouse:
    def test_row_counts_match_the_database(self, seeded):
        """The whole value of a profile is being an accurate statement about
        the data, so this is the assertion that matters most."""
        profile = profile_table(get_schema()["refunds"])
        actual = execute_readonly("SELECT count(*) FROM refunds").rows[0][0]
        assert profile.rows == actual

    def test_every_column_is_profiled(self, seeded):
        table = get_schema()["orders"]
        profile = profile_table(table)
        assert {column.name for column in profile.column_profiles} == {
            column.name for column in table.columns
        }

    def test_a_primary_key_is_fully_distinct(self, seeded):
        profile = profile_table(get_schema()["customers"])
        identifier = next(c for c in profile.column_profiles if c.name == "id")
        assert identifier.cardinality_ratio == pytest.approx(1.0)
        assert identifier.null_count == 0

    def test_a_controlled_vocabulary_is_summarised_by_its_values(self, seeded):
        """Eight refund reasons is a list worth showing; 10,000 customer names
        is not, and the profile has to tell them apart."""
        profile = profile_table(get_schema()["refunds"])
        reason = next(c for c in profile.column_profiles if c.name == "refund_reason")
        assert reason.distinct_count == 8
        assert len(reason.top_values) == 8
        assert sum(count for _, count in reason.top_values) == profile.rows

    def test_date_ranges_match_the_loaded_window(self, seeded):
        profile = profile_table(get_schema()["orders"])
        order_date = next(c for c in profile.column_profiles if c.name == "order_date")
        assert str(order_date.minimum) == "2024-09-01"
        assert str(order_date.maximum) == "2026-08-31"

    def test_the_seeded_warehouse_has_no_orphans_or_duplicates(self, seeded):
        """The seeder asserts both after every load. If the profiler disagrees
        with it, one of the two is wrong and it matters which."""
        profile = profile_warehouse()
        serious = [
            finding
            for table in profile.tables
            for finding in table.findings
            if finding.severity in ("error", "warning")
        ]
        assert not serious, [f.detail for f in serious]

    def test_total_rows_sums_the_tables(self, seeded):
        profile = profile_warehouse()
        assert profile.total_rows == sum(table.rows for table in profile.tables)
        assert profile.total_rows > 0

    def test_profiling_a_subset_touches_only_those_tables(self, seeded):
        profile = profile_warehouse(["regions"])
        assert [table.table for table in profile.tables] == ["regions"]
