"""Spawn real Conductor processes so tests can SIGKILL / SIGSTOP them."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from tests.conftest import BACKEND_DIR, eventually

# Aggressive timings so recovery happens in a couple of seconds, not minutes.
CHAOS_ENV = {
    "CONDUCTOR_LEASE_TTL_S": "1.5",
    "CONDUCTOR_HEARTBEAT_INTERVAL_S": "0.25",
    "CONDUCTOR_POLL_INTERVAL_S": "0.2",
    "CONDUCTOR_REAPER_INTERVAL_S": "0.2",
    "CONDUCTOR_WORKER_UNHEALTHY_AFTER_S": "1",
    "CONDUCTOR_WORKER_DEAD_AFTER_S": "3",
    "CONDUCTOR_SHUTDOWN_GRACE_S": "1",
    "CONDUCTOR_LOG_FORMAT": "json",
}


@dataclass
class Proc:
    name: str
    popen: subprocess.Popen[bytes]
    log: Path
    worker_id: UUID | None = None

    @property
    def pid(self) -> int:
        return self.popen.pid

    def signal(self, sig: signal.Signals) -> None:
        os.kill(self.pid, sig)


@dataclass
class Cluster:
    """Starts processes against the test database and cleans them all up."""

    database_url: str
    tmp_path: Path
    extra_env: dict[str, str] = field(default_factory=dict)
    procs: list[Proc] = field(default_factory=list)

    def _spawn(self, name: str, *args: str) -> Proc:
        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("CONDUCTOR_")},
            **CHAOS_ENV,
            **self.extra_env,
            "CONDUCTOR_DATABASE_URL": self.database_url,
        }
        log = self.tmp_path / f"{name}.log"
        # The child inherits its own copy of the descriptor; ours can close at once.
        with log.open("wb") as out:
            popen = subprocess.Popen(  # noqa: S603 - fixed argv, our own module
                [sys.executable, "-m", "conductor", *args],
                cwd=BACKEND_DIR,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        proc = Proc(name=name, popen=popen, log=log)
        self.procs.append(proc)
        return proc

    async def worker(self, db: asyncpg.Connection, name: str, concurrency: int = 2) -> Proc:
        """Start a worker and wait until it has registered."""
        proc = self._spawn(name, "worker", "--concurrency", str(concurrency))

        async def registered() -> UUID | None:
            if proc.popen.poll() is not None:
                pytest.fail(f"{name} exited early:\n{proc.log.read_text()}")
            value = await db.fetchval("SELECT id FROM workers WHERE pid = $1", proc.pid)
            return UUID(str(value)) if value else None

        proc.worker_id = await eventually(registered, within=15, what=f"{name} to register")
        return proc

    def api(self, port: int) -> Proc:
        self.extra_env["CONDUCTOR_API_PORT"] = str(port)
        return self._spawn("api", "api")

    def scheduler(self) -> Proc:
        count = sum(1 for p in self.procs if p.name.startswith("scheduler"))
        return self._spawn(f"scheduler-{count}", "scheduler")

    def stop_all(self) -> None:
        for proc in self.procs:
            if proc.popen.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.signal(signal.SIGCONT)  # a stopped process cannot handle SIGTERM
                    proc.signal(signal.SIGTERM)
        for proc in self.procs:
            try:
                proc.popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.popen.kill()
                proc.popen.wait()

    def logs(self) -> str:
        return "\n".join(f"===== {p.name} =====\n{p.log.read_text()}" for p in self.procs)


@pytest.fixture
def cluster(
    database_url: str, db: asyncpg.Connection, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[Cluster]:
    c = Cluster(database_url=database_url, tmp_path=tmp_path)
    try:
        yield c
    finally:
        c.stop_all()
        report = getattr(request.node, "rep_call", None)
        if report is not None and report.failed:
            print(c.logs())  # surfaced by pytest only for failed tests


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report
