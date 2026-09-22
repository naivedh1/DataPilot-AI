"""Tests for deterministic confidence.

The property that matters most here is the one the brief demands: no input
from the language model reaches the level. Every case below sets only facts
the run produced — retry counts, validation outcomes, row counts — and the
level follows from them.
"""

from __future__ import annotations

import pytest

from app.services.confidence import ConfidenceLevel, assess
from app.services.validation import CheckStatus, ValidationCheck, ValidationReport


def _validation(
    *, reconciliation: CheckStatus = CheckStatus.PASSED, shape_ok: bool = True
) -> ValidationReport:
    return ValidationReport(
        (
            ValidationCheck(
                "result_not_empty",
                CheckStatus.PASSED if shape_ok else CheckStatus.FAILED,
                "",
            ),
            ValidationCheck("group_reconciliation", reconciliation, "detail"),
        )
    )


def _clean(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": "success",
        "row_count": 12,
        "metric_matched": True,
        "ambiguous": False,
        "retry_count": 0,
        "validation": _validation(),
    }
    base.update(overrides)
    return base


class TestHighRequiresEverything:
    def test_a_fully_clean_run_is_high(self):
        assert assess(**_clean()).level is ConfidenceLevel.HIGH  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("metric_matched", False, ConfidenceLevel.MEDIUM),
            ("retry_count", 1, ConfidenceLevel.MEDIUM),
            ("ambiguous", True, ConfidenceLevel.LOW),
            ("row_count", 0, ConfidenceLevel.INSUFFICIENT_DATA),
        ],
    )
    def test_each_defect_lowers_the_ceiling(self, field, value, expected):
        assert assess(**_clean(**{field: value})).level is expected  # type: ignore[arg-type]


class TestCeilingSemantics:
    def test_the_worst_signal_wins(self):
        """Confidence is a ceiling, not an average: one serious problem is not
        offset by several things having gone right."""
        result = assess(**_clean(ambiguous=True, retry_count=0, metric_matched=True))  # type: ignore[arg-type]
        assert result.level is ConfidenceLevel.LOW

    def test_multiple_defects_take_the_lowest(self):
        result = assess(**_clean(ambiguous=True, row_count=0))  # type: ignore[arg-type]
        assert result.level is ConfidenceLevel.INSUFFICIENT_DATA

    def test_level_is_never_raised_by_good_signals(self):
        result = assess(**_clean(retry_count=3, metric_matched=True, row_count=9_999))  # type: ignore[arg-type]
        assert result.level is ConfidenceLevel.MEDIUM


class TestValidationInfluence:
    def test_failed_shape_check_gives_low(self):
        result = assess(**_clean(validation=_validation(shape_ok=False)))  # type: ignore[arg-type]
        assert result.level is ConfidenceLevel.LOW

    def test_failed_reconciliation_gives_low(self):
        result = assess(  # type: ignore[arg-type]
            **_clean(validation=_validation(reconciliation=CheckStatus.FAILED))
        )
        assert result.level is ConfidenceLevel.LOW

    def test_skipped_reconciliation_caps_at_medium(self):
        """An unverified claim is not a verified one. Skipping the cross-check
        must not read the same as passing it."""
        result = assess(  # type: ignore[arg-type]
            **_clean(validation=_validation(reconciliation=CheckStatus.SKIPPED))
        )
        assert result.level is ConfidenceLevel.MEDIUM

    def test_absent_validation_caps_at_medium(self):
        result = assess(**_clean(validation=None))  # type: ignore[arg-type]
        assert result.level is ConfidenceLevel.MEDIUM


class TestTerminalStates:
    def test_error_is_insufficient_data(self):
        assert (
            assess(**_clean(status="error")).level is ConfidenceLevel.INSUFFICIENT_DATA  # type: ignore[arg-type]
        )

    def test_abstention_is_insufficient_data(self):
        """Declining to answer is the correct outcome, and it must never be
        reported as a confident one."""
        result = assess(**_clean(status="no_query", row_count=0))  # type: ignore[arg-type]
        assert result.level is ConfidenceLevel.INSUFFICIENT_DATA


class TestExplainability:
    def test_rationale_names_the_limiting_signal(self):
        result = assess(**_clean(retry_count=2))  # type: ignore[arg-type]
        assert "repair" in result.rationale

    def test_high_says_so_plainly(self):
        assert "passed" in assess(**_clean()).rationale  # type: ignore[arg-type]

    def test_every_signal_is_reported(self):
        """The UI shows why, so each input must survive into the payload."""
        payload = assess(**_clean()).to_dict()  # type: ignore[arg-type]
        names = {signal["name"] for signal in payload["signals"]}
        assert names >= {
            "result_has_rows",
            "question_unambiguous",
            "metric_defined",
            "sql_correct_first_time",
            "result_checks_passed",
            "figures_reconcile",
        }

    def test_levels_serialise_as_plain_strings(self):
        payload = assess(**_clean(row_count=0)).to_dict()  # type: ignore[arg-type]
        assert payload["level"] == "insufficient_data"
        assert all(isinstance(signal["ceiling"], str) for signal in payload["signals"])


class TestOrdering:
    def test_levels_compare_by_severity_not_alphabetically(self):
        """StrEnum compares as text, which would make "low" beat "high"."""
        result = assess(**_clean(ambiguous=True, metric_matched=False))  # type: ignore[arg-type]
        assert result.level is ConfidenceLevel.LOW
