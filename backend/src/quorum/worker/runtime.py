"""The worker process: claim, execute, heartbeat, report.

Three concurrent loops share one event loop:

* **claim loop**: whenever there is a free slot, lease ready tasks and start
  executing them. It idles until woken by NOTIFY, a finished task, or the
  poll interval, whichever comes first.
* **heartbeat loop**: renews every lease still held, records liveness, and
  cancels any execution whose lease was taken over, so a slow worker stops
  wasting effort on work that now belongs to someone else.
* **listener**: a dedicated connection LISTENing for task-ready hints.

The database stays the authority throughout. If this process is killed at
any instant, its leases simply expire and the reaper recovers the tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from sqlalchemy.ext.asyncio import AsyncEngine

from quorum.core.config import Settings
from quorum.core.db import asyncpg_dsn
from quorum.core.logging import bind_log_context
from quorum.domain.refs import RefError, find_refs, resolve_refs
from quorum.domain.retry import FailureKind
from quorum.store import runs as run_store
from quorum.store import tasks as task_store
from quorum.store import workers as worker_store
from quorum.store.events import TASK_READY_CHANNEL, EventType, record_event
from quorum.store.tasks import ClaimedTask, StaleLeaseError
from quorum.worker.registry import (
    InvalidParamsError,
    PermanentTaskError,
    TaskContext,
    TaskEnvironment,
    TaskRegistry,
    UnknownTaskTypeError,
)

logger = logging.getLogger(__name__)

_MAX_ERROR_MESSAGE = 2000


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Timing and capacity knobs; see :class:`quorum.core.config.Settings`."""

    concurrency: int = 4
    lease_ttl_s: float = 30.0
    heartbeat_interval_s: float = 5.0
    poll_interval_s: float = 2.0
    shutdown_grace_s: float = 20.0
    listen: bool = True
    env: TaskEnvironment = field(default_factory=TaskEnvironment)
    # Public demo pool only: obey kill commands and injected task faults.
    demo_chaos: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> WorkerConfig:
        return cls(
            env=TaskEnvironment(
                http_allowlist=frozenset(
                    h.strip().lower() for h in settings.http_allowlist.split(",") if h.strip()
                ),
                http_timeout_s=settings.http_timeout_s,
                http_max_bytes=settings.http_max_bytes,
            ),
            demo_chaos=settings.demo_chaos,
            concurrency=settings.worker_concurrency,
            lease_ttl_s=settings.lease_ttl_s,
            heartbeat_interval_s=settings.heartbeat_interval_s,
            poll_interval_s=settings.poll_interval_s,
            shutdown_grace_s=settings.shutdown_grace_s,
            listen=settings.worker_listen,
        )


@dataclass(slots=True)
class _Inflight:
    claim: ClaimedTask
    task: asyncio.Task[None] = field(repr=False)
    # Set once the outcome is being written: the heartbeat stops renewing it,
    # and a missing renewal then no longer means "taken over".
    reporting: bool = False


class Worker:
    """One worker process's runtime. Call :meth:`run`; stop with :meth:`request_stop`."""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        registry: TaskRegistry,
        config: WorkerConfig,
        database_url: str,
        worker_id: UUID | None = None,
    ) -> None:
        self.id = worker_id or uuid4()
        self._engine = engine
        self._registry = registry
        self._config = config
        self._dsn = asyncpg_dsn(database_url)
        self._inflight: dict[UUID, _Inflight] = {}
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._rng = random.Random()
        # How a demo kill command ends the process; tests substitute this.
        self.hard_exit: Callable[[int], object] = os._exit

    # ------------------------------------------------------------------ lifecycle

    def request_stop(self) -> None:
        """Begin a graceful shutdown: stop claiming, drain, release, exit."""
        if not self._stopping.is_set():
            logger.info("worker stopping", extra={"inflight": len(self._inflight)})
            self._stopping.set()
            self._wake.set()

    async def run(self) -> None:
        """Run until :meth:`request_stop` is called and in-flight work is drained."""
        with bind_log_context(worker_id=str(self.id)):
            async with self._engine.begin() as conn:
                await worker_store.register_worker(
                    conn,
                    worker_id=self.id,
                    hostname=socket.gethostname(),
                    pid=os.getpid(),
                    concurrency=self._config.concurrency,
                    capabilities=self._registry.names(),
                    accepts_chaos=self._config.demo_chaos,
                )
            logger.info(
                "worker started",
                extra={
                    "concurrency": self._config.concurrency,
                    "task_types": self._registry.names(),
                },
            )
            helpers = [asyncio.create_task(self._heartbeat_loop(), name="heartbeat")]
            if self._config.listen:
                helpers.append(asyncio.create_task(self._listen_loop(), name="listen"))
            try:
                await self._claim_loop()
                await self._drain()
            finally:
                for helper in helpers:
                    helper.cancel()
                await asyncio.gather(*helpers, return_exceptions=True)
                with contextlib.suppress(Exception):
                    async with self._engine.begin() as conn:
                        await worker_store.mark_stopped(conn, worker_id=self.id)
                logger.info("worker stopped")

    # ------------------------------------------------------------------ claiming

    async def _claim_loop(self) -> None:
        while not self._stopping.is_set():
            # Clear before claiming: a wake-up that arrives mid-claim stays set,
            # so the wait below returns at once instead of missing it.
            self._wake.clear()
            free = self._config.concurrency - len(self._inflight)
            if free > 0:
                try:
                    async with self._engine.begin() as conn:
                        claimed = await task_store.claim_tasks(
                            conn,
                            worker_id=self.id,
                            limit=free,
                            lease_ttl_s=self._config.lease_ttl_s,
                        )
                except Exception:
                    logger.exception("claim failed; retrying after poll interval")
                    claimed = []
                for claim in claimed:
                    self._start(claim)
                if claimed and len(claimed) == free:
                    continue  # slots full; the next pass waits for one to free up
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._config.poll_interval_s):
                    await self._wake.wait()

    def _start(self, claim: ClaimedTask) -> None:
        task = asyncio.create_task(self._execute(claim), name=f"task-{claim.id}")
        self._inflight[claim.id] = _Inflight(claim=claim, task=task)

    # ------------------------------------------------------------------ execution

    async def _execute(self, claim: ClaimedTask) -> None:
        with bind_log_context(
            task_id=str(claim.id), run_id=str(claim.run_id), attempt=str(claim.attempt)
        ):
            try:
                await self._execute_and_report(claim)
            finally:
                self._inflight.pop(claim.id, None)
                self._wake.set()

    async def _execute_and_report(self, claim: ClaimedTask) -> None:
        started = time.perf_counter()
        logger.info("task started", extra={"task_type": claim.task_type})
        try:
            task_type = self._registry.get(claim.task_type)
            raw_params: dict[str, Any] = claim.params
            if next(find_refs(raw_params), None) is not None:
                async with self._engine.connect() as conn:
                    upstream = await run_store.upstream_results(conn, claim.run_id)
                raw_params = resolve_refs(raw_params, upstream)
            params = self._registry.parse_params(claim.task_type, raw_params)
            ctx = TaskContext(
                task_id=claim.id,
                run_id=claim.run_id,
                attempt=claim.attempt,
                max_attempts=claim.max_attempts,
                idempotency_key=claim.idempotency_key,
                worker_id=self.id,
                engine=self._engine,
                env=self._config.env,
            )
            if self._config.demo_chaos and claim.chaos:
                await _inject_chaos(claim)
            async with asyncio.timeout(claim.timeout_s):
                result = await task_type.handler(ctx, params)
        except asyncio.CancelledError:
            # Lease lost or shutdown. Whoever cancelled us owns what happens next.
            logger.info("task execution cancelled")
            raise
        except TimeoutError:
            await self._report_failure(
                claim, FailureKind.TIMEOUT, {"message": f"timed out after {claim.timeout_s}s"}
            )
        except (PermanentTaskError, UnknownTaskTypeError, InvalidParamsError, RefError) as exc:
            await self._report_failure(claim, FailureKind.PERMANENT, _describe(exc))
        except Exception as exc:
            await self._report_failure(claim, FailureKind.ERROR, _describe(exc))
        else:
            if self._config.demo_chaos:
                await self._obey_pending_kill()
            await self._report_success(claim, result, time.perf_counter() - started)

    def _mark_reporting(self, claim: ClaimedTask) -> None:
        entry = self._inflight.get(claim.id)
        if entry is not None:
            entry.reporting = True

    async def _report_success(
        self, claim: ClaimedTask, result: dict[str, Any], duration_s: float
    ) -> None:
        self._mark_reporting(claim)
        try:
            async with self._engine.begin() as conn:
                try:
                    await task_store.complete_task(
                        conn,
                        task_id=claim.id,
                        worker_id=self.id,
                        fencing_token=claim.fencing_token,
                        result=result,
                    )
                except StaleLeaseError:
                    # Commit the stale_write_rejected event; the result is discarded.
                    logger.warning("lease lost before completion; result discarded")
                    return
        except Exception:
            logger.exception("could not record success; the lease will expire and be reaped")
            return
        logger.info("task succeeded", extra={"duration_ms": round(duration_s * 1000, 1)})

    async def _report_failure(
        self, claim: ClaimedTask, kind: FailureKind, error: dict[str, Any]
    ) -> None:
        self._mark_reporting(claim)
        try:
            async with self._engine.begin() as conn:
                try:
                    outcome = await task_store.fail_task(
                        conn,
                        task_id=claim.id,
                        worker_id=self.id,
                        fencing_token=claim.fencing_token,
                        kind=kind,
                        error=error,
                        rng=self._rng,
                    )
                except StaleLeaseError:
                    logger.warning("lease lost before failure was recorded")
                    return
        except Exception:
            logger.exception("could not record failure; the lease will expire and be reaped")
            return
        logger.warning(
            "task failed",
            extra={
                "kind": kind.value,
                "outcome": outcome.status.value,
                "retry_in_s": outcome.delay_s,
                "error": error.get("message"),
            },
        )

    # ------------------------------------------------------------------ heartbeat

    async def _heartbeat_loop(self) -> None:
        last_renewed = time.monotonic()
        while True:
            await asyncio.sleep(self._config.heartbeat_interval_s)
            held = {tid: entry for tid, entry in self._inflight.items() if not entry.reporting}
            try:
                async with self._engine.begin() as conn:
                    renewed = await task_store.renew_leases(
                        conn,
                        worker_id=self.id,
                        held=[(tid, e.claim.fencing_token) for tid, e in held.items()],
                        lease_ttl_s=self._config.lease_ttl_s,
                    )
                    await worker_store.heartbeat(
                        conn, worker_id=self.id, current_load=len(self._inflight)
                    )
                    commands = (
                        await worker_store.take_commands(conn, worker_id=self.id)
                        if self._config.demo_chaos
                        else []
                    )
                last_renewed = time.monotonic()
                if "kill" in commands:
                    self._crash()
                    return
            except Exception:
                logger.exception("heartbeat failed")
                if time.monotonic() - last_renewed > self._config.lease_ttl_s:
                    # We cannot prove we still hold anything: stop all work.
                    # (Fencing already blocks our writes; this saves the effort.)
                    for entry in list(self._inflight.values()):
                        entry.task.cancel()
                continue
            for task_id in held.keys() - renewed:
                lost = self._inflight.get(task_id)
                if lost is not None and not lost.reporting:
                    await self._abandon_lost_lease(lost)

    def _crash(self) -> None:
        """A real crash, on purpose: no lease release, no cleanup, no goodbye.

        Exactly what the reaper exists to recover from.
        """
        logger.critical("demo: killed by chaos command; exiting immediately")
        logging.shutdown()
        self.hard_exit(137)

    async def _obey_pending_kill(self) -> None:
        """Demo only: crash *before* reporting a result if a kill is pending.

        Heartbeats only look for commands every interval, so a short task could
        otherwise finish between the kill and the next heartbeat, and the
        visitor who pressed "kill" would see the task complete on the worker
        they just killed. Checking here makes "kill mid-task" deterministic.
        """
        async with self._engine.begin() as conn:
            commands = await worker_store.take_commands(conn, worker_id=self.id)
        if "kill" in commands:
            self._crash()

    async def _abandon_lost_lease(self, entry: _Inflight) -> None:
        claim = entry.claim
        with bind_log_context(task_id=str(claim.id), run_id=str(claim.run_id)):
            logger.warning("lease lost; cancelling execution")
        entry.task.cancel()
        with contextlib.suppress(Exception):
            async with self._engine.begin() as conn:
                await record_event(
                    conn,
                    run_id=claim.run_id,
                    task_id=claim.id,
                    type=EventType.LEASE_LOST,
                    payload={"worker_id": str(self.id), "fencing_token": claim.fencing_token},
                )

    # ------------------------------------------------------------------ listen

    async def _listen_loop(self) -> None:
        """Keep a LISTEN connection open; reconnect on failure. Best effort only."""

        def on_notify(*_: object) -> None:
            self._wake.set()

        backoff = 0.5
        while True:
            conn: asyncpg.Connection | None = None
            closed = asyncio.Event()
            try:
                conn = await asyncpg.connect(self._dsn)
                conn.add_termination_listener(lambda _, done=closed: done.set())
                await conn.add_listener(TASK_READY_CHANNEL, on_notify)
                backoff = 0.5
                self._wake.set()  # catch anything enqueued while we were disconnected
                await closed.wait()
                logger.warning("LISTEN connection closed; reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("LISTEN connection failed; polling only", extra={"error": repr(exc)})
            finally:
                if conn is not None and not conn.is_closed():
                    with contextlib.suppress(Exception):
                        await conn.close(timeout=2)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    # ------------------------------------------------------------------ shutdown

    async def _drain(self) -> None:
        if not self._inflight:
            return
        pending = [entry.task for entry in self._inflight.values()]
        logger.info("draining in-flight tasks", extra={"count": len(pending)})
        _, still_running = await asyncio.wait(pending, timeout=self._config.shutdown_grace_s)
        if not still_running:
            return
        unfinished = [e.claim for e in self._inflight.values() if not e.reporting]
        for task in still_running:
            task.cancel()
        await asyncio.gather(*still_running, return_exceptions=True)
        for claim in unfinished:
            with contextlib.suppress(Exception):
                async with self._engine.begin() as conn:
                    released = await task_store.release_task(
                        conn, task_id=claim.id, worker_id=self.id, fencing_token=claim.fencing_token
                    )
                logger.info(
                    "released task on shutdown",
                    extra={"task_id": str(claim.id), "released": released},
                )


class InjectedFailureError(RuntimeError):
    """A failure injected by the public demo (retryable, like any crash)."""


async def _inject_chaos(claim: ClaimedTask) -> None:
    """Apply demo fault injection: pace the step, then maybe fail on purpose."""
    chaos = claim.chaos or {}
    delay = float(chaos.get("delay_s", 0) or 0)
    if delay > 0:
        await asyncio.sleep(min(delay, 30.0))
    if claim.attempt <= int(chaos.get("fail_attempts", 0) or 0):
        raise InjectedFailureError(
            f"injected by the demo: attempt {claim.attempt} fails on purpose"
        )


def _describe(exc: BaseException) -> dict[str, Any]:
    return {"type": type(exc).__name__, "message": str(exc)[:_MAX_ERROR_MESSAGE]}
