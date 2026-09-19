"""Seed the DataPilot AI warehouse.

    python -m scripts.seed_database              # create + seed if empty
    python -m scripts.seed_database --reset      # destroy and rebuild
    python -m scripts.seed_database --seed 7     # different random seed
    python -m scripts.seed_database --dry-run    # generate, report, load nothing

Safety: the command refuses to touch a warehouse that already holds data unless
`--reset` is passed explicitly. Nothing is ever destroyed silently.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace

import psycopg

from app.core.config import Settings, get_settings
from app.database.bootstrap import (
    apply_readonly_grants,
    ensure_roles_and_database,
    verify_readonly_privileges,
)
from app.database.engine import create_admin_engine, to_libpq
from app.database.seed import integrity
from app.database.seed.config import GenerationConfig
from app.database.seed.dataset import GeneratedDataset
from app.database.seed.generator import generate
from app.database.seed.loader import is_populated, load, table_counts, truncate_all
from app.models import Base

logger = logging.getLogger("seed")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed_database",
        description="Create and populate the DataPilot AI analytics warehouse.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="DESTRUCTIVE: truncate all warehouse tables before loading.",
    )
    parser.add_argument("--seed", type=int, default=None, help="Random seed (default: 42).")
    parser.add_argument(
        "--customers", type=int, default=None, help="Number of customers to generate."
    )
    parser.add_argument("--orders", type=int, default=None, help="Number of orders to generate.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and report, but write nothing to the database.",
    )
    parser.add_argument("--verbose", action="store_true", help="Emit debug-level logging.")
    return parser.parse_args(argv)


def _build_config(args: argparse.Namespace) -> GenerationConfig:
    """Apply CLI overrides on top of the pinned defaults.

    Built explicitly rather than by splatting a dict into `replace()`: the
    fields have different types, so a `dict[str, int]` cannot describe them and
    the call would not type-check.
    """
    config = GenerationConfig()
    return replace(
        config,
        seed=args.seed if args.seed is not None else config.seed,
        n_customers=(args.customers if args.customers is not None else config.n_customers),
        n_orders=args.orders if args.orders is not None else config.n_orders,
    )


def _validate_configuration(settings: Settings) -> list[str]:
    """Check that the credentials seeding needs are actually present."""
    problems: list[str] = []
    if not settings.postgres_superuser_password.get_secret_value():
        problems.append("POSTGRES_SUPERUSER_PASSWORD is not set")
    if not settings.postgres_admin_password.get_secret_value():
        problems.append("POSTGRES_ADMIN_PASSWORD is not set")
    if not settings.postgres_readonly_password.get_secret_value():
        problems.append("POSTGRES_READONLY_PASSWORD is not set")
    return problems


def _print_summary(dataset: GeneratedDataset, elapsed: float) -> None:
    print("\nGenerated")
    print("-" * 64)
    for table, count in dataset.counts().items():
        print(f"  {table:<14} {count:>10,}")
    print(f"  {'elapsed':<14} {elapsed:>9.1f}s")


def _print_statistics(conn: psycopg.Connection) -> None:
    """Print a few headline figures so a run is self-evidently sane."""
    queries: list[tuple[str, str]] = [
        ("date range", "SELECT min(order_date) || ' to ' || max(order_date) FROM orders"),
        (
            "completed revenue",
            "SELECT to_char(SUM(total_amount), 'FM999,999,999,990.00') "
            "FROM orders WHERE status = 'completed'",
        ),
        (
            "average order value",
            "SELECT to_char(AVG(total_amount), 'FM999,990.00') "
            "FROM orders WHERE status = 'completed'",
        ),
        ("distinct months", "SELECT count(DISTINCT date_trunc('month', order_date)) FROM orders"),
        (
            "avg lines per order",
            "SELECT to_char(AVG(n), 'FM990.00') FROM "
            "(SELECT count(*) AS n FROM order_items GROUP BY order_id) s",
        ),
    ]
    print("\nWarehouse statistics")
    print("-" * 64)
    with conn.cursor() as cur:
        for label, query in queries:
            cur.execute(query)
            row = cur.fetchone()
            print(f"  {label:<22} {row[0] if row else 'n/a'}")

        cur.execute(
            "SELECT status, count(*), "
            "to_char(100.0 * count(*) / SUM(count(*)) OVER (), 'FM990.0') "
            "FROM orders GROUP BY status ORDER BY count(*) DESC"
        )
        print("\n  order status mix")
        for status, count, pct in cur.fetchall():
            print(f"    {status:<12} {count:>8,}  {pct:>5}%")

        cur.execute(
            "SELECT c.customer_segment, count(*), "
            "to_char(AVG(o.total_amount), 'FM999,990.00') "
            "FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE o.status = 'completed' GROUP BY 1 ORDER BY 2 DESC"
        )
        print("\n  orders and AOV by segment")
        for segment, count, aov in cur.fetchall():
            print(f"    {segment:<12} {count:>8,}  AOV {aov:>12}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    settings = get_settings()
    config = _build_config(args)

    print("DataPilot AI - warehouse seeding")
    print("=" * 64)

    # 1. Configuration ------------------------------------------------------
    problems = _validate_configuration(settings)
    if problems:
        print("\nConfiguration problems:")
        for problem in problems:
            print(f"  - {problem}")
        print("\nCopy .env.example to .env and fill in the database passwords.")
        return 2
    print(f"  target      {settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}")
    print(f"  seed        {config.seed}")
    print(f"  window      {config.window_start} to {config.window_end}")

    # 2. Generate (before touching the database, so a bug costs nothing) -----
    started = time.perf_counter()
    dataset = generate(config)
    _print_summary(dataset, time.perf_counter() - started)

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        _print_anomalies(dataset)
        return 0

    # 3. Roles and database -------------------------------------------------
    report = ensure_roles_and_database(settings)
    print("\nBootstrap")
    print("-" * 64)
    print(f"  database created        {report.database_created}")
    print(f"  admin role created      {report.admin_role_created}")
    print(f"  readonly role created   {report.readonly_role_created}")

    # 4. Schema -------------------------------------------------------------
    engine = create_admin_engine(settings)
    Base.metadata.create_all(engine)
    engine.dispose()
    print(f"  tables ensured          {len(Base.metadata.tables)}")

    # 5. Load ---------------------------------------------------------------
    admin_dsn = to_libpq(settings.admin_dsn)
    with psycopg.connect(admin_dsn) as conn:
        if is_populated(conn):
            if not args.reset:
                existing = table_counts(conn)
                print("\nWarehouse already contains data:")
                for table, count in existing.items():
                    if count:
                        print(f"  {table:<14} {count:>10,}")
                print("\nRefusing to overwrite it. Re-run with --reset to rebuild from scratch.")
                return 3
            truncate_all(conn)

        load_started = time.perf_counter()
        written = load(conn, dataset)
        conn.commit()
        load_elapsed = time.perf_counter() - load_started

    print(f"\nLoaded in {load_elapsed:.1f}s")
    print("-" * 64)
    for table, count in written.items():
        print(f"  {table:<14} {count:>10,}")

    # 6. Grants -------------------------------------------------------------
    apply_readonly_grants(settings)
    privilege_problems = verify_readonly_privileges(settings)
    print("\nSecurity")
    print("-" * 64)
    if privilege_problems:
        for problem in privilege_problems:
            print(f"  FAIL  {problem}")
    else:
        print(f"  PASS  {settings.postgres_readonly_user}: SELECT only, no DDL/DML")

    # 7. Integrity ----------------------------------------------------------
    with psycopg.connect(admin_dsn) as conn:
        results = integrity.run_all(conn, config.window_start, config.window_end)
        print("\nIntegrity checks")
        print("-" * 64)
        for result in results:
            print(f"  {result.symbol}  {result.name}")
            print(f"        {result.detail}")
        _print_statistics(conn)

    _print_anomalies(dataset)

    failed = [r for r in results if not r.passed] or privilege_problems
    print("\n" + "=" * 64)
    if failed:
        print("SEEDING COMPLETED WITH FAILURES - see above.")
        return 1
    print("Seeding complete. All integrity and security checks passed.")
    return 0


def _print_anomalies(dataset: GeneratedDataset) -> None:
    """Report planted anomalies.

    Printed by the operator-facing seeding command only. These are never
    exposed through the API — discovering them is the agent's job.
    """
    print("\nPlanted anomalies (operator view; not exposed via the API)")
    print("-" * 64)
    for anomaly in dataset.anomalies:
        print(f"  {anomaly.key}")
        print(f"      {anomaly.detail}")


if __name__ == "__main__":
    sys.exit(main())
