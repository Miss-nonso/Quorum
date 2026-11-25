"""Demo support endpoints (Phase 6 adds the chaos controls here)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from conductor.samples import DEMO_DATASET

router = APIRouter(prefix="/demo", tags=["demo"])


@router.get("/dataset", summary="The sample workflow's input data")
async def dataset() -> list[dict[str, Any]]:
    """A small, fixed list of orders that the sample workflow's ``fetch`` step downloads.

    Served by the API itself so the demo needs no third-party service.
    """
    return DEMO_DATASET
