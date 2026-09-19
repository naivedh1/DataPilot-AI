"""SQL safety validation.

This is the first line of defence. The read-only database role is the second,
and the one that still holds if this code is wrong — but a system that relies
solely on database permissions gives the user a raw privilege error instead of a
useful message, and cannot stop an expensive-but-permitted query at all.

**Validation is AST-based, never regex.** `sqlglot` parses the statement into a
syntax tree and the tree is inspected. The difference is not stylistic:

* `SELECT 'we should not DROP TABLE users'` is a harmless string literal. A
  regex for `DROP TABLE` rejects it.
* `SELECT 1;/*x*/DELETE FROM orders` hides a second statement behind a comment.
  A regex for a leading `SELECT` accepts it.
* `SELECT * FROM orders WHERE note = 'a; DROP TABLE x'` contains a semicolon
  inside a literal. Splitting on `;` mangles it.

A parser gets all three right because it understands the grammar. Every one of
those cases is in the test suite.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

logger = logging.getLogger(__name__)

DIALECT = "postgres"

#: Expression types that write, alter, or administer. Rejected outright.
#: Checked by class, so any syntax that parses to one of these is caught
#: regardless of how it was spelled.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Grant,
    exp.TruncateTable,
    exp.Merge,
    exp.Copy,
    exp.Command,  # sqlglot's catch-all: VACUUM, CALL, SET, REVOKE, ...
)

#: Functions that read or write the filesystem, execute code, or leak
#: connection details. None has a legitimate use in an analytics query.
FORBIDDEN_FUNCTIONS = frozenset(
    {
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "dblink",
        "dblink_connect",
        "query_to_xml",
        "pg_read_server_files",
        "set_config",
        "current_setting",
        "pg_logical_emit_message",
    }
)

#: System catalogs and schemas. Blocked because they expose roles, password
#: hashes and server configuration — none of which is business analytics.
FORBIDDEN_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast"})

FORBIDDEN_TABLE_PREFIXES = ("pg_",)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The outcome of validating one statement."""

    is_valid: bool
    #: Why it was rejected. Safe to show a user and useful to the repair loop.
    reason: str = ""
    #: Tables the statement reads, lowercased.
    tables: frozenset[str] = field(default_factory=frozenset)
    #: Normalised SQL, with a LIMIT applied when one was missing.
    normalized_sql: str = ""
    #: True when the validator added or lowered a LIMIT.
    limit_applied: bool = False

    def __bool__(self) -> bool:
        return self.is_valid


def _reject(reason: str) -> ValidationResult:
    return ValidationResult(is_valid=False, reason=reason)


def _statement_tables(tree: exp.Expression) -> set[str]:
    """Every real table the statement reads.

    CTE names are excluded: `WITH monthly AS (...) SELECT * FROM monthly` reads
    `orders`, not a table called `monthly`, and treating the alias as a table
    would make the allow-list check reject valid SQL.
    """
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE) if cte.alias_or_name}
    tables: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in cte_names:
            continue
        schema = (table.db or "").lower()
        tables.add(f"{schema}.{name}" if schema else name)
    return tables


def validate(
    sql: str,
    *,
    allowed_tables: frozenset[str] | set[str] | None = None,
    max_rows: int | None = None,
) -> ValidationResult:
    """Check that `sql` is a single, safe, read-only statement.

    When `allowed_tables` is supplied, every referenced table must appear in it.
    This is what catches a hallucinated relation before the database does, and
    it is why the SQL generator is given the retrieved schema: the same table
    list validates the result.

    When `max_rows` is supplied, a `LIMIT` is added if absent or lowered if
    higher, and the normalised SQL is returned.
    """
    if not sql or not sql.strip():
        return _reject("The query is empty.")

    # Parse the whole input. `parse` returns every statement it finds, so a
    # smuggled second statement shows up here rather than at the database.
    try:
        statements = sqlglot.parse(sql, dialect=DIALECT)
    except ParseError as error:
        return _reject(f"The query could not be parsed: {_first_line(str(error))}")

    statements = [statement for statement in statements if statement is not None]
    if not statements:
        return _reject("The query contains no statement.")
    if len(statements) > 1:
        return _reject(f"Only one statement is allowed; this contains {len(statements)}.")

    tree = statements[0]

    # -- shape -------------------------------------------------------------
    if not isinstance(tree, exp.Select | exp.Union | exp.Except | exp.Intersect | exp.Subquery):
        return _reject(
            f"Only SELECT queries are permitted; this is a {type(tree).__name__.upper()} statement."
        )

    # -- forbidden node types, anywhere in the tree -------------------------
    # Checked over the whole tree, not just the root: a write can hide inside a
    # CTE (`WITH x AS (DELETE ... RETURNING *) SELECT * FROM x`), which is a
    # genuine PostgreSQL feature and a genuine escape route.
    for node_type in FORBIDDEN_NODES:
        found = tree.find(node_type)
        if found is not None:
            return _reject(
                f"{_describe(node_type)} statements are not permitted. This assistant is read-only."
            )

    # -- INTO would write a new relation ------------------------------------
    if tree.find(exp.Into) is not None:
        return _reject("SELECT ... INTO is not permitted; it creates a table.")

    # -- locking clauses would hold row locks -------------------------------
    if tree.find(exp.Lock) is not None:
        return _reject("Locking clauses (FOR UPDATE / FOR SHARE) are not permitted.")

    # -- dangerous functions -------------------------------------------------
    for function in tree.find_all(exp.Anonymous, exp.Func):
        name = _function_name(function)
        if name and name.lower() in FORBIDDEN_FUNCTIONS:
            return _reject(f"The function {name}() is not permitted.")

    # -- system catalogs -----------------------------------------------------
    tables = _statement_tables(tree)
    for qualified in tables:
        schema, _, bare = qualified.rpartition(".")
        if schema and schema in FORBIDDEN_SCHEMAS:
            return _reject(f"The schema {schema} is not accessible.")
        if bare.startswith(FORBIDDEN_TABLE_PREFIXES):
            return _reject(f"System catalog {bare} is not accessible.")

    # -- table allow-list ----------------------------------------------------
    if allowed_tables is not None:
        permitted = {name.lower() for name in allowed_tables}
        unknown = {
            qualified.rpartition(".")[2]
            for qualified in tables
            if qualified.rpartition(".")[2] not in permitted
        }
        if unknown:
            return _reject(
                f"Unknown table(s): {', '.join(sorted(unknown))}. "
                f"Available tables: {', '.join(sorted(permitted))}."
            )

    # -- row limit -----------------------------------------------------------
    normalized, limit_applied = _apply_limit(tree, max_rows)

    return ValidationResult(
        is_valid=True,
        tables=frozenset(qualified.rpartition(".")[2] for qualified in tables),
        normalized_sql=normalized,
        limit_applied=limit_applied,
    )


def _apply_limit(tree: exp.Expression, max_rows: int | None) -> tuple[str, bool]:
    """Add or tighten the statement's LIMIT.

    Modifying the AST rather than appending text: appending ` LIMIT n` to a
    query that already ends in `LIMIT 10` is a syntax error, and appending to a
    UNION binds to its last branch rather than to the whole result.
    """
    if max_rows is None:
        return tree.sql(dialect=DIALECT, pretty=True), False

    existing = tree.args.get("limit")
    if existing is not None:
        current = existing.expression
        if isinstance(current, exp.Literal) and current.is_int and int(current.name) <= max_rows:
            return tree.sql(dialect=DIALECT, pretty=True), False

    # `.limit()` is defined on the query node types, not on the Expression
    # base class. Validation has already established the tree is one of them;
    # narrowing here (rather than asserting) keeps the guarantee under `-O`,
    # where asserts are stripped out entirely.
    if not isinstance(tree, exp.Select | exp.Union | exp.Except | exp.Intersect | exp.Subquery):
        return tree.sql(dialect=DIALECT, pretty=True), False
    limited = tree.limit(max_rows)
    return limited.sql(dialect=DIALECT, pretty=True), True


def _function_name(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Anonymous):
        return str(node.this) if node.this else None
    name = getattr(node, "sql_name", None)
    return name() if callable(name) else None


def _describe(node_type: type[exp.Expression]) -> str:
    return {
        exp.Insert: "INSERT",
        exp.Update: "UPDATE",
        exp.Delete: "DELETE",
        exp.Drop: "DROP",
        exp.Alter: "ALTER",
        exp.Create: "CREATE",
        exp.Grant: "GRANT",
        exp.TruncateTable: "TRUNCATE",
        exp.Merge: "MERGE",
        exp.Copy: "COPY",
        exp.Command: "Administrative",
    }.get(node_type, node_type.__name__.upper())


def _first_line(message: str) -> str:
    return message.strip().splitlines()[0][:200]
