"""Tests for the error contract: safe messages out, technical detail in logs."""

from __future__ import annotations

from http import HTTPStatus

import pytest

from app.core import exceptions as exc


def test_detail_defaults_to_safe_message_when_omitted():
    error = exc.SQLValidationError()
    assert error.detail == error.safe_message


def test_technical_detail_is_kept_separate_from_the_safe_message():
    """The whole point: the client sees one string, the log sees another."""
    error = exc.SQLExecutionError(
        detail='relation "ordrs" does not exist at character 15',
    )
    assert "ordrs" in error.detail
    assert "ordrs" not in error.safe_message


def test_safe_message_can_be_overridden_for_actionable_feedback():
    error = exc.SQLValidationError(
        detail="statement type DELETE is not permitted",
        safe_message="Only read-only queries are allowed.",
    )
    assert error.safe_message == "Only read-only queries are allowed."
    assert error.detail == "statement type DELETE is not permitted"


def test_a_leaked_connection_string_does_not_reach_the_safe_message():
    """Guards the rule that driver errors are never forwarded verbatim."""
    leaky = "could not connect: postgresql://admin:hunter2@db:5432/datapilot"
    error = exc.SQLExecutionError(detail=leaky)
    assert "hunter2" not in error.safe_message


@pytest.mark.parametrize(
    ("error_cls", "expected_status"),
    [
        (exc.DataPilotError, HTTPStatus.INTERNAL_SERVER_ERROR),
        (exc.ConfigurationError, HTTPStatus.INTERNAL_SERVER_ERROR),
        (exc.LLMError, HTTPStatus.BAD_GATEWAY),
        (exc.SQLValidationError, HTTPStatus.BAD_REQUEST),
        (exc.SQLExecutionError, HTTPStatus.BAD_REQUEST),
        (exc.QueryTimeoutError, HTTPStatus.GATEWAY_TIMEOUT),
        (exc.QueryNotFoundError, HTTPStatus.NOT_FOUND),
    ],
)
def test_errors_map_to_the_intended_http_status(error_cls, expected_status):
    assert error_cls.status_code == expected_status


def test_timeout_is_a_kind_of_execution_error():
    """Callers handling SQLExecutionError must also catch timeouts."""
    assert issubclass(exc.QueryTimeoutError, exc.SQLExecutionError)


def test_all_error_codes_are_unique():
    """Codes are a machine-readable contract; duplicates break client handling."""
    subclasses = [
        cls
        for cls in vars(exc).values()
        if isinstance(cls, type) and issubclass(cls, exc.DataPilotError)
    ]
    codes = [cls.code for cls in subclasses]
    assert len(codes) == len(set(codes)), f"duplicate error codes: {codes}"


def test_every_error_is_catchable_as_the_base_type():
    with pytest.raises(exc.DataPilotError):
        raise exc.QueryTimeoutError("statement timeout after 10000ms")
