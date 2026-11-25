"""Idempotency keys: the basis of effectively-once side effects.

Delivery is at-least-once, so a task may execute more than once. Side-effecting
tasks record their effect under the task's idempotency key with a uniqueness
constraint, turning repeat executions into no-ops. See docs/semantics.md.
"""

from __future__ import annotations

import re
from uuid import UUID

MAX_KEY_LENGTH = 200
_KEY_PATTERN = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII, no spaces


def derive_idempotency_key(run_id: UUID, node_key: str) -> str:
    """Default key: unique per (run, node), stable across retries and replays."""
    return validate_idempotency_key(f"{run_id}:{node_key}")


def validate_idempotency_key(key: str) -> str:
    """Return ``key`` if it is acceptable as an override, else raise ``ValueError``."""
    if not 1 <= len(key) <= MAX_KEY_LENGTH or not _KEY_PATTERN.match(key):
        raise ValueError(
            f"idempotency key must be 1-{MAX_KEY_LENGTH} printable ASCII characters without spaces"
        )
    return key
