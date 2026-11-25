"""Command-line entry point: ``quorum <command>``.

One image, several process types: ``api``, ``worker``, ``scheduler``, plus
the one-shot ``migrate`` and ``seed``.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import signal
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from quorum.core.config import Settings, get_settings
from quorum.core.logging import configure_logging

# Run from the backend project root (locally) or /app (in the image), where
# alembic.ini and the alembic/ directory live.
_ALEMBIC_INI = Path("alembic.ini")


def _run_api(settings: Settings, _: argparse.Namespace) -> None:
    import uvicorn

    from quorum.api.app import create_app

    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # keep our JSON logging configuration
        proxy_headers=True,
        # SSE streams never end on their own, so without a bound uvicorn's
        # graceful shutdown would wait on them forever. Clients reconnect
        # (with Last-Event-ID) to another instance and lose nothing.
        timeout_graceful_shutdown=math.ceil(settings.shutdown_grace_s),
    )


def _run_migrate(_: Settings, __: argparse.Namespace) -> None:
    from alembic import command
    from alembic.config import Config

    if not _ALEMBIC_INI.is_file():
        raise SystemExit(f"{_ALEMBIC_INI.resolve()} not found; run from the backend directory.")
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")


async def _serve_until_signalled(
    start: Callable[[], Coroutine[Any, Any, None]], stop: Callable[[], None]
) -> None:
    """Run ``start()``; on SIGTERM/SIGINT call ``stop()`` and let it wind down."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)
    await start()


def _run_worker(settings: Settings, args: argparse.Namespace) -> None:
    from quorum.core.db import create_engine
    from quorum.tasks import builtin_registry
    from quorum.worker.runtime import Worker, WorkerConfig

    if args.concurrency is not None:
        settings = settings.model_copy(update={"worker_concurrency": args.concurrency})

    async def main() -> None:
        # Pool: one connection per task slot, plus heartbeat and claim loops.
        engine = create_engine(
            settings.model_copy(update={"db_pool_size": settings.worker_concurrency + 2})
        )
        worker = Worker(
            engine=engine,
            registry=builtin_registry(),
            config=WorkerConfig.from_settings(settings),
            database_url=settings.database_url,
        )
        try:
            await _serve_until_signalled(worker.run, worker.request_stop)
        finally:
            await engine.dispose()

    asyncio.run(main())


def _run_scheduler(settings: Settings, _: argparse.Namespace) -> None:
    from quorum.core.db import create_engine
    from quorum.scheduler.service import Scheduler, SchedulerConfig

    async def main() -> None:
        engine = create_engine(settings)
        scheduler = Scheduler(
            engine=engine,
            database_url=settings.database_url,
            config=SchedulerConfig.from_settings(settings),
        )
        try:
            await _serve_until_signalled(scheduler.run, scheduler.request_stop)
        finally:
            await engine.dispose()

    asyncio.run(main())


def _run_seed(settings: Settings, _: argparse.Namespace) -> None:
    from quorum.core.db import create_engine
    from quorum.seed import seed

    async def main() -> None:
        engine = create_engine(settings)
        try:
            await seed(engine, dataset_url=settings.demo_dataset_url)
        finally:
            await engine.dispose()

    asyncio.run(main())


def _run_all(settings: Settings, args: argparse.Namespace) -> None:
    """API + scheduler + supervised workers in one container (free hosting tiers)."""
    import uvicorn

    from quorum.allinone import create_all_in_one_app
    from quorum.core.db import create_engine
    from quorum.seed import seed

    # Platforms like Render choose the port and expect 0.0.0.0.
    port = int(os.environ.get("PORT") or settings.api_port)
    host = "0.0.0.0" if os.environ.get("PORT") else settings.api_host  # noqa: S104
    if "QUORUM_DEMO_DATASET_URL" not in os.environ:
        # The sample's fetch step calls this same container.
        settings = settings.model_copy(
            update={"demo_dataset_url": f"http://127.0.0.1:{port}/api/v1/demo/dataset"}
        )

    _run_migrate(settings, args)

    async def seed_once() -> None:
        engine = create_engine(settings)
        try:
            await seed(engine, dataset_url=settings.demo_dataset_url)
        finally:
            await engine.dispose()

    asyncio.run(seed_once())
    workers = args.workers if args.workers is not None else settings.all_in_one_workers
    uvicorn.run(
        create_all_in_one_app(settings, workers=workers),
        host=host,
        port=port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*" if os.environ.get("PORT") else None,
        timeout_graceful_shutdown=math.ceil(settings.shutdown_grace_s),
    )


_COMMANDS: dict[str, Callable[[Settings, argparse.Namespace], None]] = {
    "api": _run_api,
    "worker": _run_worker,
    "scheduler": _run_scheduler,
    "migrate": _run_migrate,
    "seed": _run_seed,
    "all": _run_all,
}


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and run the selected process type."""
    parser = argparse.ArgumentParser(prog="quorum", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("api", help="Serve the HTTP API.")
    worker = sub.add_parser("worker", help="Claim and execute tasks.")
    worker.add_argument("--concurrency", type=int, help="Task slots (default from settings).")
    sub.add_parser(
        "scheduler", help="Run the reaper (and, in later phases, cron and run advancement)."
    )
    sub.add_parser("migrate", help="Apply database migrations up to head.")
    sub.add_parser("seed", help="Create the sample workflow if it does not exist.")
    everything = sub.add_parser(
        "all", help="Migrate, seed, then run API + scheduler + supervised workers in one container."
    )
    everything.add_argument("--workers", type=int, help="Worker processes (default from settings).")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    _COMMANDS[args.command](settings, args)


if __name__ == "__main__":
    main()
