"""Data profiling endpoint.

Answers "what is actually in here?" — row counts, null rates, cardinality,
duplicate candidates, orphaned foreign keys, numeric outliers.

Profiling reads every table, so it is deliberately a separate endpoint rather
than something the query path runs implicitly. Nothing here is cached: a
profile that silently ages is worse than one that takes a moment, because its
whole value is being an accurate statement about the data right now.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas.query import ProfileResponse
from app.services.profiler import profile_warehouse

router = APIRouter(tags=["profile"])


@router.get(
    "/profile",
    response_model=ProfileResponse,
    summary="Measured statistics and data-quality findings for the warehouse",
)
def get_profile_endpoint(
    tables: list[str] | None = Query(
        default=None,
        description="Limit the profile to these tables. Omit to profile all of them.",
    ),
) -> ProfileResponse:
    profile = profile_warehouse(tables)
    return ProfileResponse.model_validate(profile.to_dict())
