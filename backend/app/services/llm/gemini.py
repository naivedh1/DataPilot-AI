"""Gemini provider, built on the `google-genai` SDK.

Structured output uses Gemini's native JSON mode with a response schema derived
from the Pydantic model, rather than asking for JSON in the prompt and hoping.
The model is constrained by the decoder, so malformed JSON is not a failure mode
that needs handling — only *semantically* wrong JSON is, and that is what
Pydantic validation catches.

Retries cover transient failures only. A validation error is not retried with
the same prompt (it would fail identically); it is surfaced so the caller — the
agent graph — can decide whether repair is worthwhile.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, TypeVar

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError, LLMError
from app.services.llm.base import LLMProvider, LLMResponse, LLMUsage

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

#: Transient conditions worth retrying. A 400 means the request is wrong and
#: will stay wrong, so retrying it only wastes the user's time.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.75

#: Ceiling on a single wait, so honouring a server-supplied delay cannot stall
#: a request for minutes.
_MAX_BACKOFF_SECONDS = 30.0


class GeminiProvider(LLMProvider):
    """Live Gemini backend."""

    name = "gemini"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        if not self._settings.llm_configured:
            raise ConfigurationError(
                detail="GEMINI_API_KEY is unset or still the .env.example placeholder",
                safe_message="The AI model is not configured.",
            )
        self._client = genai.Client(api_key=self._settings.gemini_api_key.get_secret_value())

    # -- internals ---------------------------------------------------------

    def _config(
        self,
        *,
        system: str,
        temperature: float | None,
        max_output_tokens: int | None,
        response_schema: type[BaseModel] | None = None,
    ) -> genai_types.GenerateContentConfig:
        settings = self._settings
        kwargs: dict[str, Any] = {
            "system_instruction": system,
            "temperature": (temperature if temperature is not None else settings.llm_temperature),
            "max_output_tokens": (
                max_output_tokens
                if max_output_tokens is not None
                else settings.llm_max_output_tokens
            ),
        }
        if response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = response_schema
        return genai_types.GenerateContentConfig(**kwargs)

    def _generate(
        self, prompt: str, config: genai_types.GenerateContentConfig
    ) -> tuple[str, LLMUsage]:
        """Call the API with bounded retries on transient failures."""
        started = time.perf_counter()
        last_error: Exception | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = self._client.models.generate_content(
                    model=self._settings.gemini_model,
                    contents=prompt,
                    config=config,
                )
                break
            except genai_errors.APIError as error:
                last_error = error
                status = getattr(error, "code", None)

                # A daily quota is not a transient failure. Retrying one burns
                # the user's time for a condition that will not clear for hours,
                # so it is treated as terminal and reported as what it is.
                daily_quota = status == 429 and _is_daily_quota(error)

                if daily_quota or status not in _RETRYABLE_STATUS or attempt == _MAX_ATTEMPTS:
                    # Log the status, never the prompt: prompts embed user data
                    # and retrieved schema.
                    logger.warning(
                        "gemini call failed (status=%s, attempt=%s/%s)",
                        status,
                        attempt,
                        _MAX_ATTEMPTS,
                    )
                    # A 404 here means the configured model name is wrong or
                    # retired, not that the request was bad. Saying so plainly
                    # turns a dead end into a one-line configuration fix.
                    if status == 404:
                        safe = (
                            f"The configured AI model ({self._settings.gemini_model}) "
                            "is not available for this API key. Set GEMINI_MODEL "
                            "in .env to a currently available model."
                        )
                    elif daily_quota:
                        safe = (
                            "The daily quota for this API key has been used up. "
                            "It resets on Google's schedule, or a paid tier "
                            "removes the limit."
                        )
                    elif status in _RETRYABLE_STATUS:
                        safe = "The AI model is temporarily unavailable. Please retry."
                    else:
                        safe = "The AI model could not process this request."

                    raise LLMError(
                        detail=f"Gemini API error (status={status}): {error}",
                        safe_message=safe,
                    ) from error
                # Prefer the server's own RetryInfo. Exponential backoff from
                # 0.75s waits under two seconds for a rate limit whose response
                # explicitly says "retry in 40s", guaranteeing the retry also
                # fails and wasting the attempt budget.
                delay = _retry_delay_seconds(error)
                if delay is None:
                    delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                delay = min(delay, _MAX_BACKOFF_SECONDS)
                logger.info(
                    "gemini transient failure (status=%s), retrying in %.1fs",
                    status,
                    delay,
                )
                time.sleep(delay)
        else:  # pragma: no cover - the loop always breaks or raises
            raise LLMError(detail=f"exhausted retries: {last_error}")

        latency_ms = (time.perf_counter() - started) * 1000
        text = (response.text or "").strip()
        if not text:
            raise LLMError(
                detail=f"empty response; finish reason: {_finish_reason(response)}",
                safe_message="The AI model returned an empty response.",
            )

        return text, LLMUsage(
            input_tokens=_usage_field(response, "prompt_token_count"),
            output_tokens=_usage_field(response, "candidates_token_count"),
            latency_ms=round(latency_ms, 2),
            model=self._settings.gemini_model,
        )

    # -- interface ---------------------------------------------------------

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        text, usage = self._generate(
            prompt,
            self._config(
                system=system,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            ),
        )
        return LLMResponse(text=text, usage=usage)

    def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[ModelT],
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> tuple[ModelT, LLMUsage]:
        text, usage = self._generate(
            prompt,
            self._config(
                system=system,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                response_schema=schema,
            ),
        )
        try:
            return schema.model_validate_json(text), usage
        except ValidationError as error:
            # Native JSON mode makes malformed syntax very unlikely, so this
            # almost always means the model returned well-formed JSON that does
            # not satisfy the schema's constraints.
            logger.warning("gemini structured output failed validation: %s", error)
            raise LLMError(
                detail=f"response did not match {schema.__name__}: {error}",
                safe_message="The AI model returned an unexpected response format.",
            ) from error
        except json.JSONDecodeError as error:  # pragma: no cover - JSON mode
            raise LLMError(detail=f"invalid JSON: {error}") from error


def _error_payload(error: Exception) -> dict[str, Any]:
    """The structured body of an API error, if the SDK exposed one."""
    for attribute in ("details", "response_json"):
        value = getattr(error, attribute, None)
        if isinstance(value, dict):
            return value
    return {}


def _is_daily_quota(error: Exception) -> bool:
    """Whether a 429 is a per-day quota rather than a per-minute rate limit.

    Google returns both as 429. The distinction matters: a per-minute limit
    clears in seconds and is worth retrying, while a per-day quota will not
    clear for hours and retrying it only delays an inevitable failure.
    """
    text = str(error)
    if "PerDay" in text or "per day" in text.lower():
        return True

    payload = _error_payload(error)
    for detail in payload.get("error", {}).get("details", []):
        if not isinstance(detail, dict):
            continue
        for violation in detail.get("violations", []):
            if "PerDay" in str(violation.get("quotaId", "")):
                return True
    return False


def _retry_delay_seconds(error: Exception) -> float | None:
    """The server's requested retry delay, in seconds, if it supplied one."""
    match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", str(error))
    if match:
        return float(match.group(1))
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", str(error))
    return float(match.group(1)) if match else None


def _usage_field(response: Any, field: str) -> int:
    metadata = getattr(response, "usage_metadata", None)
    value = getattr(metadata, field, None) if metadata else None
    return int(value) if value else 0


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        return str(getattr(candidates[0], "finish_reason", "unknown"))
    return "no candidates"
