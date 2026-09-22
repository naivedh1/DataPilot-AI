"""Deterministic confidence.

The model does not get a say in how confident the system is. It cannot: a
model that wrote a query on a misread of the question will report the same
cheerful 0.9 it reports when it got everything right, because self-assessed
confidence measures fluency, not correctness. Before this module the agent
carried exactly that — an LLM-supplied float, and a hardcoded 0.55 from the
offline provider.

What replaces it is arithmetic over facts the run already produced: whether
the question resolved to a metric the semantic layer defines, whether the SQL
passed validation first time or had to be repaired, whether the result
survived the checks in `validation.py`, whether the grouped figures reconcile
against an independent total.

The model is a **ceiling**, not a score. Every run starts at HIGH and is
lowered by whatever went wrong; nothing can raise it back. This matters
because the failures compose in one direction — a reconciliation mismatch is
not offset by the query having parsed cleanly — and because a ceiling is
explainable. Each downgrade records the signal that caused it, so the answer
can say *why* it is medium rather than merely that it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.services.validation import CheckStatus, ValidationReport


class ConfidenceLevel(StrEnum):
    """How much weight the answer can bear.

    Ordered worst to best so `min` composes downgrades.
    """

    INSUFFICIENT_DATA = "insufficient_data"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: Rank for comparison. StrEnum compares as text, which would order these
#: alphabetically and silently make "low" the best outcome.
_RANK: dict[ConfidenceLevel, int] = {
    ConfidenceLevel.INSUFFICIENT_DATA: 0,
    ConfidenceLevel.LOW: 1,
    ConfidenceLevel.MEDIUM: 2,
    ConfidenceLevel.HIGH: 3,
}


@dataclass(frozen=True, slots=True)
class ConfidenceSignal:
    """One reason the ceiling moved, or held."""

    name: str
    #: The highest level this signal permits.
    ceiling: ConfidenceLevel
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ceiling": str(self.ceiling), "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ConfidenceAssessment:
    """The level, and the signals that produced it."""

    level: ConfidenceLevel
    signals: tuple[ConfidenceSignal, ...]

    @property
    def limiting_signals(self) -> tuple[ConfidenceSignal, ...]:
        """The signals that actually set the level, in declaration order."""
        return tuple(signal for signal in self.signals if signal.ceiling is self.level)

    @property
    def rationale(self) -> str:
        """A one-line explanation naming what held the level down."""
        if self.level is ConfidenceLevel.HIGH:
            return "Every check the system can run passed."
        reasons = [signal.detail for signal in self.limiting_signals]
        if not reasons:
            return "No supporting evidence was available."
        return " ".join(reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": str(self.level),
            "rationale": self.rationale,
            "signals": [signal.to_dict() for signal in self.signals],
        }


def _lowest(signals: list[ConfidenceSignal]) -> ConfidenceLevel:
    return min((signal.ceiling for signal in signals), key=lambda level: _RANK[level])


def assess(
    *,
    status: str,
    row_count: int,
    metric_matched: bool,
    ambiguous: bool,
    retry_count: int,
    validation: ValidationReport | None,
) -> ConfidenceAssessment:
    """Compute the confidence ceiling for one completed run.

    `status` is the agent's terminal status. `metric_matched` says whether the
    question resolved onto a metric the semantic layer actually defines, rather
    than onto column names the model guessed at. `ambiguous` is set when the
    planner could not pin the question to one reading.
    """
    signals: list[ConfidenceSignal] = []

    # -- blocking conditions ------------------------------------------------
    if status == "error":
        signals.append(
            ConfidenceSignal(
                "run_completed",
                ConfidenceLevel.INSUFFICIENT_DATA,
                "The run did not complete, so there is no answer to be confident about.",
            )
        )
    elif status == "no_query":
        signals.append(
            ConfidenceSignal(
                "answerable_from_warehouse",
                ConfidenceLevel.INSUFFICIENT_DATA,
                "The warehouse does not hold the data this question needs.",
            )
        )
    elif row_count == 0:
        signals.append(
            ConfidenceSignal(
                "result_has_rows",
                ConfidenceLevel.INSUFFICIENT_DATA,
                "The query returned no rows, so nothing can be concluded from it.",
            )
        )
    else:
        signals.append(
            ConfidenceSignal(
                "result_has_rows", ConfidenceLevel.HIGH, f"The query returned {row_count} rows."
            )
        )

    # -- ambiguity ----------------------------------------------------------
    signals.append(
        ConfidenceSignal(
            "question_unambiguous",
            ConfidenceLevel.LOW if ambiguous else ConfidenceLevel.HIGH,
            (
                "The question has more than one reasonable reading, so the "
                "figure answers one interpretation of it."
                if ambiguous
                else "The question resolved to a single interpretation."
            ),
        )
    )

    # -- semantic resolution ------------------------------------------------
    signals.append(
        ConfidenceSignal(
            "metric_defined",
            ConfidenceLevel.HIGH if metric_matched else ConfidenceLevel.MEDIUM,
            (
                "The measure is one the semantic layer defines."
                if metric_matched
                else (
                    "No defined metric matched this question, so the measure was "
                    "inferred from column names rather than a business definition."
                )
            ),
        )
    )

    # -- SQL repair ---------------------------------------------------------
    signals.append(
        ConfidenceSignal(
            "sql_correct_first_time",
            ConfidenceLevel.HIGH if retry_count == 0 else ConfidenceLevel.MEDIUM,
            (
                "The SQL validated and ran on the first attempt."
                if retry_count == 0
                else (
                    f"The SQL needed {retry_count} repair "
                    f"{'attempt' if retry_count == 1 else 'attempts'}; the query that "
                    "finally ran may not answer the original question as written."
                )
            ),
        )
    )

    # -- result validation --------------------------------------------------
    signals.extend(_validation_signals(validation))

    return ConfidenceAssessment(_lowest(signals), tuple(signals))


def _validation_signals(validation: ValidationReport | None) -> list[ConfidenceSignal]:
    """Turn the validation report into confidence signals.

    Reconciliation is separated from the shape checks deliberately. A failed
    shape check says one number looks wrong; a failed reconciliation says the
    answer is built from a different set of rows than its own total, which is
    a defect in the query rather than in a single figure.

    A reconciliation pass is treated as supporting HIGH, but it is a narrow
    guarantee — it shows the grouped rows partition the same data, not that
    the query is right. `validation.py` documents the boundary.
    """
    if validation is None:
        return [
            ConfidenceSignal(
                "result_validated",
                ConfidenceLevel.MEDIUM,
                "The result was not validated, so its arithmetic is unverified.",
            )
        ]

    signals: list[ConfidenceSignal] = []

    reconciliation = next(
        (check for check in validation.checks if check.name == "group_reconciliation"), None
    )
    other_failures = [check for check in validation.failed if check.name != "group_reconciliation"]

    if other_failures:
        names = ", ".join(check.name for check in other_failures)
        signals.append(
            ConfidenceSignal(
                "result_checks_passed",
                ConfidenceLevel.LOW,
                f"The result failed {len(other_failures)} validation check(s): {names}.",
            )
        )
    else:
        signals.append(
            ConfidenceSignal(
                "result_checks_passed",
                ConfidenceLevel.HIGH,
                "Every applicable result check passed.",
            )
        )

    if reconciliation is None or reconciliation.status is CheckStatus.SKIPPED:
        signals.append(
            ConfidenceSignal(
                "figures_reconcile",
                ConfidenceLevel.MEDIUM,
                (
                    "The figures were not cross-checked against an independent "
                    "total, so groups missing from the answer would not have been "
                    "detected."
                ),
            )
        )
    elif reconciliation.status is CheckStatus.FAILED:
        signals.append(
            ConfidenceSignal(
                "figures_reconcile",
                ConfidenceLevel.LOW,
                f"The parts do not sum to the independent total ({reconciliation.detail}).",
            )
        )
    else:
        signals.append(
            ConfidenceSignal(
                "figures_reconcile",
                ConfidenceLevel.HIGH,
                "The grouped figures sum to an independently computed total.",
            )
        )

    return signals
