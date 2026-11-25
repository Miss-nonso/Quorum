"""Seed the database with the sample workflow (idempotent)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncEngine

from quorum.samples import SAMPLE_WORKFLOW_NAME, sample_definition
from quorum.store import workflows as wf_store
from quorum.tasks import builtin_registry

logger = logging.getLogger(__name__)


async def seed(engine: AsyncEngine, *, dataset_url: str) -> bool:
    """Create the sample workflow unless it exists. Returns True if it was created."""
    definition = sample_definition(dataset_url)
    builtin_registry().validate_workflow(definition)
    async with engine.begin() as conn:
        if await wf_store.get_workflow_by_name(conn, SAMPLE_WORKFLOW_NAME) is not None:
            logger.info("sample workflow already present", extra={"workflow": SAMPLE_WORKFLOW_NAME})
            return False
        await wf_store.create_workflow(conn, name=SAMPLE_WORKFLOW_NAME, definition=definition)
    logger.info(
        "seeded sample workflow",
        extra={"workflow": SAMPLE_WORKFLOW_NAME, "dataset_url": dataset_url},
    )
    return True
