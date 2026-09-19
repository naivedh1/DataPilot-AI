"""Schema, constraint and index tests against the live warehouse."""

from __future__ import annotations

from typing import Any

import pytest

from app.models import TABLE_LOAD_ORDER

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "regions",
    "products",
    "customers",
    "employees",
    "orders",
    "order_items",
}


def _rows(conn: Any, query: str, params: tuple[Any, ...] | None = None) -> list[tuple[Any, ...]]:
    # `params` stays None rather than becoming (): passing an empty sequence
    # switches psycopg into placeholder parsing, so a literal '%price%' in the
    # SQL is read as a '%p' placeholder and the query fails to prepare.
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


class TestTables:
    def test_all_six_warehouse_tables_exist(self, admin_conn):
        found = {
            row[0]
            for row in _rows(
                admin_conn,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
            )
        }
        assert found >= EXPECTED_TABLES

    def test_load_order_matches_the_actual_tables(self):
        assert set(TABLE_LOAD_ORDER) == EXPECTED_TABLES

    @pytest.mark.parametrize("table", sorted(EXPECTED_TABLES))
    def test_every_table_has_a_primary_key(self, admin_conn, table):
        pks = _rows(
            admin_conn,
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'p'",
            (table,),
        )
        assert len(pks) == 1

    def test_money_columns_are_numeric_not_float(self, admin_conn):
        """Binary floating point cannot represent 0.10 exactly; summing float
        money drifts and would break the financial identities."""
        bad = _rows(
            admin_conn,
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND (column_name LIKE '%price%' OR column_name LIKE '%amount%'
                   OR column_name IN ('subtotal', 'line_total'))
              AND data_type NOT IN ('numeric')
            """,
        )
        assert bad == [], f"non-numeric money columns: {bad}"

    def test_created_at_is_timezone_aware(self, admin_conn):
        naive = _rows(
            admin_conn,
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema='public' AND column_name='created_at' "
            "AND data_type <> 'timestamp with time zone'",
        )
        assert naive == []


class TestForeignKeys:
    def test_expected_relationships_exist(self, admin_conn):
        found = {
            (row[0], row[1], row[2])
            for row in _rows(
                admin_conn,
                """
                SELECT tc.table_name, kcu.column_name, ccu.table_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON kcu.constraint_name = tc.constraint_name
                JOIN information_schema.constraint_column_usage ccu
                  ON ccu.constraint_name = tc.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = 'public'
                """,
            )
        }
        expected = {
            ("customers", "region_id", "regions"),
            ("employees", "region_id", "regions"),
            ("orders", "customer_id", "customers"),
            ("orders", "shipping_region_id", "regions"),
            ("order_items", "order_id", "orders"),
            ("order_items", "product_id", "products"),
        }
        assert expected <= found, f"missing: {expected - found}"

    def test_order_lines_cascade_from_their_order(self, admin_conn):
        """The one safe cascade: a line has no meaning without its order, and
        orphaned lines would corrupt every revenue total."""
        rule = _rows(
            admin_conn,
            "SELECT confdeltype FROM pg_constraint "
            "WHERE conrelid = 'order_items'::regclass AND contype = 'f' "
            "AND confrelid = 'orders'::regclass",
        )
        assert rule[0][0] == "c"  # 'c' = CASCADE

    @pytest.mark.parametrize(
        ("table", "referenced"),
        [
            ("orders", "customers"),
            ("orders", "regions"),
            ("customers", "regions"),
            ("order_items", "products"),
        ],
    )
    def test_history_bearing_relationships_restrict_deletes(self, admin_conn, table, referenced):
        """Deleting a customer, region or product must never silently destroy
        the order history that references it."""
        rules = _rows(
            admin_conn,
            "SELECT confdeltype FROM pg_constraint "
            "WHERE conrelid = %s::regclass AND contype = 'f' "
            "AND confrelid = %s::regclass",
            (table, referenced),
        )
        assert rules, f"no FK from {table} to {referenced}"
        assert all(rule[0] == "r" for rule in rules)  # 'r' = RESTRICT

    def test_foreign_keys_are_actually_enforced(self, admin_conn):
        """Declared is not the same as enforced."""
        import psycopg

        with admin_conn.cursor() as cur, pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                "INSERT INTO orders (order_number, customer_id, order_date, "
                "status, sales_channel, shipping_region_id, subtotal, "
                "discount_amount, tax_amount, shipping_amount, total_amount) "
                "VALUES ('FK-TEST-1', 999999999, '2025-01-01', 'completed', "
                "'Web', 1, 10.00, 0.00, 0.00, 0.00, 10.00)"
            )
        admin_conn.rollback()


class TestCheckConstraints:
    def test_inconsistent_line_total_is_rejected(self, admin_conn):
        """The line-total identity is enforced by the database, not just by the
        generator, so no future writer can violate it."""
        import psycopg

        with admin_conn.cursor() as cur:
            cur.execute("SELECT id FROM orders LIMIT 1")
            order_id = cur.fetchone()[0]
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO order_items (order_id, product_id, quantity, "
                    "unit_price, discount_amount, line_total) "
                    "VALUES (%s, 1, 2, 10.00, 0.00, 999.00)",  # should be 20.00
                    (order_id,),
                )
        admin_conn.rollback()

    def test_inconsistent_order_total_is_rejected(self, admin_conn):
        import psycopg

        with admin_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO orders (order_number, customer_id, order_date, "
                "status, sales_channel, shipping_region_id, subtotal, "
                "discount_amount, tax_amount, shipping_amount, total_amount) "
                "VALUES ('CK-TEST-1', 1, '2025-01-01', 'completed', 'Web', 1, "
                "100.00, 0.00, 5.00, 0.00, 999.00)"  # should be 105.00
            )
        admin_conn.rollback()

    def test_zero_quantity_is_rejected(self, admin_conn):
        import psycopg

        with admin_conn.cursor() as cur:
            cur.execute("SELECT id FROM orders LIMIT 1")
            order_id = cur.fetchone()[0]
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO order_items (order_id, product_id, quantity, "
                    "unit_price, discount_amount, line_total) "
                    "VALUES (%s, 1, 0, 10.00, 0.00, 0.00)",
                    (order_id,),
                )
        admin_conn.rollback()

    def test_unknown_order_status_is_rejected(self, admin_conn):
        """The controlled vocabulary is enforced, so the agent cannot be told a
        status exists that the data can never contain."""
        import psycopg

        with admin_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO orders (order_number, customer_id, order_date, "
                "status, sales_channel, shipping_region_id, subtotal, "
                "discount_amount, tax_amount, shipping_amount, total_amount) "
                "VALUES ('CK-TEST-2', 1, '2025-01-01', 'SHIPPED_MAYBE', 'Web', "
                "1, 10.00, 0.00, 0.00, 0.00, 10.00)"
            )
        admin_conn.rollback()

    def test_unknown_customer_segment_is_rejected(self, admin_conn):
        import psycopg

        with admin_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO customers (customer_code, first_name, last_name, "
                "email, signup_date, region_id, customer_segment, "
                "acquisition_channel, age_band) VALUES "
                "('CK-T3', 'A', 'B', 'ck3@example.com', '2025-01-01', 1, "
                "'B2B_MEGA', 'Organic', '25-34')"
            )
        admin_conn.rollback()

    def test_discount_exceeding_subtotal_is_rejected(self, admin_conn):
        import psycopg

        with admin_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO orders (order_number, customer_id, order_date, "
                "status, sales_channel, shipping_region_id, subtotal, "
                "discount_amount, tax_amount, shipping_amount, total_amount) "
                "VALUES ('CK-TEST-4', 1, '2025-01-01', 'completed', 'Web', 1, "
                "10.00, 50.00, 0.00, 0.00, -40.00)"
            )
        admin_conn.rollback()


class TestUniqueConstraints:
    @pytest.mark.parametrize(
        ("table", "column"),
        [
            ("customers", "customer_code"),
            ("customers", "email"),
            ("products", "sku"),
            ("orders", "order_number"),
            ("employees", "employee_code"),
            ("regions", "name"),
        ],
    )
    def test_business_keys_are_unique(self, admin_conn, table, column):
        found = _rows(
            admin_conn,
            """
            SELECT 1 FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid = c.conrelid
                               AND a.attnum = ANY (c.conkey)
            WHERE c.conrelid = %s::regclass AND c.contype = 'u'
              AND a.attname = %s
            """,
            (table, column),
        )
        assert found, f"{table}.{column} is not unique"


class TestIndexes:
    @pytest.mark.parametrize(
        "index",
        [
            "ix_customers_region_id",
            "ix_customers_customer_segment",
            "ix_customers_signup_date",
            "ix_orders_customer_id",
            "ix_orders_order_date",
            "ix_orders_status_order_date",
            "ix_orders_shipping_region_id",
            "ix_order_items_order_id",
            "ix_order_items_product_id",
            "ix_products_category_subcategory",
        ],
    )
    def test_analytical_index_exists(self, admin_conn, index):
        found = _rows(
            admin_conn,
            "SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname=%s",
            (index,),
        )
        assert found, f"missing index {index}"

    def test_status_filtering_is_index_backed(self, admin_conn):
        """`status` alone is four values — too low-cardinality to index usefully.
        The composite leads with it and follows with order_date, serving the
        dominant 'completed orders in a date range' pattern."""
        definition = _rows(
            admin_conn,
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_orders_status_order_date'",
        )[0][0]
        assert "status" in definition
        assert "order_date" in definition

    def test_indexes_are_not_indiscriminate(self, admin_conn):
        """Every index costs write throughput and storage. A count far above the
        number of columns would mean indexing was not a deliberate choice."""
        count = _rows(
            admin_conn,
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public'",
        )[0][0]
        assert count < 40, f"{count} indexes suggests indiscriminate indexing"
