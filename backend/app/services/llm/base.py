"""The LLM provider interface.

An abstraction over a single vendor is usually premature. Here it earns its
place for a concrete reason: **the entire system must be testable and
demonstrable without a network call or an API key.** A deterministic in-process
provider is the only way to have the agent graph, the SQL validator, the retry
loop and the API covered by fast, hermetic tests.

The interface is deliberately narrow — one method, two shapes:

* `complete()`     — free text, for insights and narrative answers.
* `complete_json()` — a validated Pydantic model, for anything the code branches
  on. Structured output is parsed and validated before it reaches the caller, so
  a malformed response fails at the boundary rather than three nodes later.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token accounting and latency for one call."""

    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    model: str = ""
    #: True when the response came from a non-network provider.
    simulated: bool = False


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A completion, with the metadata the observability layer records."""

    text: str
    usage: LLMUsage = field(default_factory=LLMUsage)


class LLMProvider(ABC):
    """A text-generation backend."""

    #: Human-readable provider name, used in logs and the health endpoint.
    name: str = "abstract"

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        prompt: str,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        """Generate free text."""

    @abstractmethod
    def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[ModelT],
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> tuple[ModelT, LLMUsage]:
        """Generate a response conforming to `schema`.

        Implementations must validate before returning. A caller receiving a
        `ModelT` may assume it is well-formed.
        """

    @property
    def is_live(self) -> bool:
        """Whether this provider makes real network calls."""
        return True
