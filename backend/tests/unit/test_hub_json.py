from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from quorum.api.hub import dumps


def test_numeric_values_stay_json_numbers() -> None:
    """Postgres numeric arrives as Decimal; a string here showed "NaN s ago" in the UI."""
    payload = json.loads(dumps({"heartbeat_age_s": Decimal("3.21"), "n": 2}))
    assert payload == {"heartbeat_age_s": 3.21, "n": 2}
    assert isinstance(payload["heartbeat_age_s"], float)


def test_dates_and_ids_are_strings() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    payload = json.loads(dumps({"at": at, "id": UUID(int=1)}))
    assert payload == {"at": "2026-01-01T00:00:00+00:00", "id": str(UUID(int=1))}
