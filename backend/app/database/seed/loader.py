"""Loading a generated dataset into PostgreSQL.

Uses `COPY ... FROM STDIN` rather than INSERT. For ~200,000 rows the difference
is roughly two orders of magnitude: COPY streams rows through a single
statement, while executemany pays per-row protocol overhead.

Loading runs in **one transaction**. Either the whole warehouse lands or none of
it does — a half-loaded warehouse would silently break every financial
aggregate.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

import psycopg
from psycopg import sql

from app.database.seed.dataset import (
    CUSTOMER_COLUMNS,
    EMPLOYEE_COLUMNS,
    ORDER_COLUMNS,
    ORDER_ITEM_COLUMNS,
    PRODUCT_COLUMNS,
    REGION_COLUMNS,
    GeneratedDataset,
)
from app.models import TABLE_LOAD_ORDER

logger = logging.getLogger(__name__)

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "regions": REGION_COLUMNS,
    "products": PRODUCT_COLUMNS,
    "customers": CUSTOMER_COLUMNS,
    "employees": EMPLOYEE_COLUMNS,
    "orders": ORDER_COLUMNS,
    "order_items": ORDER_ITEM_COLUMNS,
}


def _copy_rows(
    conn: psycopg.Connection,
    table: str,
    columns: tuple[str, ...],
    rows: Iterable[Sequence[object]],
) -> int:
    """Stream rows into `table` via COPY. Returns the number written."""
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
    )
    written = 0
    with conn.cursor() as cur, cur.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)
            written += 1
    return written


def table_counts(conn: psycopg.Connection) -> dict[str, int]:
    """Current row count for every warehouse table."""
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in TABLE_LOAD_ORDER:
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
            row = cur.fetchone()
            counts[table] = int(row[0]) if row else 0
    return counts


def is_populated(conn: psycopg.Connection) -> bool:
    """Whether any warehouse table already holds data."""
    return any(count > 0 for count in table_counts(conn).values())


def truncate_all(conn: psycopg.Connection) -> None:
    """Remove all warehouse data and reset identity sequences.

    Destructive by design, and only ever reached behind an explicit `--reset`
    flag. CASCADE is safe here because the truncation covers every table that
    could reference another.
    """
    statement = sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
        sql.SQL(", ").join(sql.Identifier(table) for table in TABLE_LOAD_ORDER)
    )
    conn.execute(statement)
    logger.info("truncated all warehouse tables")


def _resync_sequences(conn: psycopg.Connection) -> None:
    """Point each identity sequence past the highest id just loaded.

    COPY supplies explicit ids, which does not advance the sequence. Without
    this, the next ORM insert would collide on the primary key — a failure that
    would only surface much later, in Phase 3.
    """
    with conn.cursor() as cur:
        for table in TABLE_LOAD_ORDER:
            cur.execute(
                sql.SQL(
                    "SELECT setval(pg_get_serial_sequence({}, 'id'), "
                    "COALESCE((SELECT max(id) FROM {}), 1), true)"
                ).format(sql.Literal(table), sql.Identifier(table))
            )
    logger.info("identity sequences resynchronised")


def load(conn: psycopg.Connection, dataset: GeneratedDataset) -> dict[str, int]:
    """Load a dataset into an empty warehouse, in one transaction."""
    rows_by_table: dict[str, Iterable[Sequence[object]]] = {
        "regions": (region.to_row() for region in dataset.regions),
        "products": (product.to_row() for product in dataset.products),
        "customers": (customer.to_row() for customer in dataset.customers),
        "employees": (employee.to_row() for employee in dataset.employees),
        "orders": dataset.orders,
        "order_items": dataset.order_items,
    }

    written: dict[str, int] = {}
    for table in TABLE_LOAD_ORDER:
        count = _copy_rows(conn, table, TABLE_COLUMNS[table], rows_by_table[table])
        written[table] = count
        logger.info("loaded %s rows into %s", f"{count:,}", table)

    _resync_sequences(conn)
    return written
