"""Shared route dependencies.

Thin by design: the API layer's job is to translate HTTP into service calls, so
dependencies resolve collaborators rather than containing logic.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header

from app.core.config import Settings, get_settings
from app.services.conversations import ConversationStore, get_store


def request_id(
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> str:
    """Correlation id for one request.

    Honours a caller-supplied `X-Request-ID` so a trace can be followed across
    the frontend and the backend, and generates one otherwise. Truncated to
    bound the length of a value that reaches the logs.
    """
    if x_request_id:
        return x_request_id[:64]
    return uuid.uuid4().hex[:12]


SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[ConversationStore, Depends(get_store)]
RequestIdDep = Annotated[str, Depends(request_id)]
