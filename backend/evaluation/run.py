"""The evaluation runner.

    python -m evaluation.run                # all cases
    python -m evaluation.run --category security
    python -m evaluation.run --no-retries   # first-attempt quality only
    python -m evaluation.run --json report.json

Reports what was measured and nothing more. There are no derived "accuracy"
figures that cannot be traced back to a specific check on a specific case, and
no metric is reported for a dimension the suite does not actually test.

When running against the offline baseline, every line of the report says so.
Numbers from a rule-based baseline are a floor, not a model evaluation, and
presenting them as the latter would be the exact dishonesty this framework
exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.agents.runner import run_agent
from app.core.config import Settings, get_settings
from app.services.llm import get_provider
from evaluation.cases import CASES, Category, EvalCase
from evaluation.grader import CaseResult, Outcome, grade

REPORTS_DIR = Path(__file__).parent / "reports"


class QuotaExhausted(RuntimeError):
    """The provider's quota ran out mid-run.

    Raised rather than recorded as a failure. A case that never reached the
    model tells us nothing about the model, and counting it as a failure would
    report a quality number that was never measured — precisely the fabricated
    metric this framework exists to avoid.
    """


def _quota_exhausted(result: CaseResult) -> bool:
    text = f"{result.error} {' '.join(c.detail for c in result.checks)}".lower()
    return "quota" in text and ("daily" in text or "used up" in text)


def _run_case(case: EvalCase, settings: Settings) -> CaseResult:
    """Run one case, including its preceding turn if it is a follow-up."""
    history = []
    if case.preceding_question:
        first = run_agent(case.preceding_question, settings=settings)
        if first.succeeded:
            history.append(first.to_turn())

    run = run_agent(case.question, history=history, settings=settings)
    return grade(case, run)


def _summarise(results: list[CaseResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.outcome is Outcome.PASS)
    latencies = sorted(result.latency_ms for result in results)

    by_category: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = by_category.setdefault(result.category, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if result.outcome is Outcome.PASS:
            bucket["passed"] += 1

    graded_for_grounding = [r for r in results if r.grounded is not None]
    produced_sql = [r for r in results if r.sql.strip()]

    return {
        "total_cases": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "sql_produced": len(produced_sql),
        "sql_produced_rate": round(len(produced_sql) / total, 4) if total else 0.0,
        "grounded_cases": sum(1 for r in graded_for_grounding if r.grounded),
        "groundedness_checked": len(graded_for_grounding),
        "groundedness_rate": (
            round(
                sum(1 for r in graded_for_grounding if r.grounded) / len(graded_for_grounding),
                4,
            )
            if graded_for_grounding
            else None
        ),
        "total_retries": sum(result.retries for result in results),
        "cases_needing_retry": sum(1 for r in results if r.retries > 0),
        "latency_p50_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "latency_p95_ms": (
            round(latencies[int(len(latencies) * 0.95) - 1], 1) if latencies else 0.0
        ),
        "latency_max_ms": round(max(latencies), 1) if latencies else 0.0,
        "by_category": by_category,
    }


def _print_report(
    results: list[CaseResult], summary: dict[str, Any], simulated: bool, elapsed: float
) -> None:
    width = 78
    print("\n" + "=" * width)
    print("DataPilot AI - evaluation report")
    print("=" * width)

    if simulated:
        print()
        print("  PROVIDER: deterministic offline baseline (no GEMINI_API_KEY).")
        print("  These figures measure the rule-based floor, NOT a language model.")
        print("  Set GEMINI_API_KEY to evaluate the model.")

    print(f"\n  cases           {summary['total_cases']}")
    print(f"  passed          {summary['passed']}")
    print(f"  failed          {summary['failed']}")
    print(f"  pass rate       {float(summary['pass_rate']) * 100:.1f}%")
    print(f"  produced SQL    {summary['sql_produced']}/{summary['total_cases']}")
    if summary["groundedness_rate"] is not None:
        print(
            f"  groundedness    {float(summary['groundedness_rate']) * 100:.1f}% "
            f"({summary['grounded_cases']}/{summary['groundedness_checked']} checked)"
        )
    print(
        f"  retries         {summary['total_retries']} across "
        f"{summary['cases_needing_retry']} cases"
    )
    print(f"  latency p50     {summary['latency_p50_ms']:.0f} ms")
    print(f"  latency p95     {summary['latency_p95_ms']:.0f} ms")
    print(f"  wall clock      {elapsed:.1f} s")

    print("\n" + "-" * width)
    print("  By category")
    print("-" * width)
    by_category: dict[str, dict[str, int]] = summary["by_category"]
    for category in sorted(by_category):
        stats = by_category[category]
        rate = stats["passed"] / stats["total"] * 100
        bar = "#" * int(rate / 5)
        print(f"  {category:<16} {stats['passed']:>2}/{stats['total']:<2} {rate:>5.0f}%  {bar}")

    failures = [result for result in results if result.outcome is not Outcome.PASS]
    if failures:
        print("\n" + "-" * width)
        print(f"  Failures ({len(failures)})")
        print("-" * width)
        for result in failures:
            print(f"\n  [{result.case_id}] {result.question}")
            for check in result.failed_checks:
                print(f"      FAILED: {check.name}")
                if check.detail:
                    print(f"              {check.detail[:110]}")
            if result.error:
                print(f"      error: {result.error[:110]}")

    print("\n" + "=" * width)
    verdict = "ALL CASES PASSED" if not failures else f"{len(failures)} CASE(S) FAILED"
    print(f"  {verdict}")
    print("=" * width + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.run",
        description="Score DataPilot AI against the benchmark question set.",
    )
    parser.add_argument(
        "--category",
        choices=[category.value for category in Category],
        help="Run only one category.",
    )
    parser.add_argument("--case", help="Run a single case by id.")
    parser.add_argument(
        "--no-retries",
        action="store_true",
        help="Disable SQL repair, to measure first-attempt quality.",
    )
    parser.add_argument("--json", type=Path, help="Write the full report as JSON.")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-case output.")
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.no_retries:
        from dataclasses import replace as _replace  # noqa: F401

        settings = settings.model_copy(update={"sql_max_retries": 0})

    cases = list(CASES)
    if args.category:
        cases = [case for case in cases if case.category.value == args.category]
    if args.case:
        cases = [case for case in cases if case.id == args.case]
    if not cases:
        print("No cases matched.", file=sys.stderr)
        return 2

    provider = get_provider()
    simulated = not provider.is_live

    print(f"Running {len(cases)} evaluation cases against {provider.name}...")
    if args.no_retries:
        print("SQL repair disabled: measuring first-attempt quality.")

    started = time.perf_counter()
    results: list[CaseResult] = []
    aborted = False
    for index, case in enumerate(cases, start=1):
        result = _run_case(case, settings)

        if _quota_exhausted(result):
            print(
                f"\n  Stopped after {index - 1} of {len(cases)} cases: the "
                f"provider's quota is exhausted.\n"
                "  The remaining cases were never sent to the model, so they "
                "are not counted.\n"
                "  Partial results below cover only what actually ran.",
                file=sys.stderr,
            )
            aborted = True
            break

        results.append(result)
        if not args.quiet:
            mark = "PASS" if result.outcome is Outcome.PASS else "FAIL"
            print(f"  [{index:>2}/{len(cases)}] {mark}  {case.id:<9} {case.question[:52]}")
    elapsed = time.perf_counter() - started

    if not results:
        print(
            "\nNo cases completed - the provider was unavailable for every "
            "attempt. No metrics are reported, because none were measured.",
            file=sys.stderr,
        )
        return 2

    summary = _summarise(results)
    summary["cases_attempted"] = len(results)
    summary["cases_total"] = len(cases)
    summary["run_complete"] = not aborted
    _print_report(results, summary, simulated, elapsed)

    if aborted:
        print(
            f"  PARTIAL RUN: {len(results)} of {len(cases)} cases completed. "
            "These figures describe the cases that ran, not the full benchmark.\n"
        )

    if args.json:
        payload = {
            "provider": provider.name,
            "simulated": simulated,
            "retries_enabled": not args.no_retries,
            "run_complete": not aborted,
            "cases_attempted": len(results),
            "cases_defined": len(cases),
            "summary": summary,
            "results": [asdict(result) for result in results],
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"Report written to {args.json}")

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
