"""Command-line entry point: ``conductor <command>``.

One image, several process types: ``api``, ``worker``, ``scheduler``, plus
the one-shot ``migrate`` and ``seed``.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import signal
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from conductor.core.config import Settings, get_settings
from conductor.core.logging import configure_logging

# Run from the backend project root (locally) or /app (in the image), where
# alembic.ini and the alembic/ directory live.
_ALEMBIC_INI = Path("alembic.ini")


def _run_api(settings: Settings, _: argparse.Namespace) -> None:
    import uvicorn

    from conductor.api.app import create_app

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
    from conductor.core.db import create_engine
    from conductor.tasks import builtin_registry
    from conductor.worker.runtime import Worker, WorkerConfig

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
    from conductor.core.db import create_engine
    from conductor.scheduler.service import Scheduler, SchedulerConfig

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
    from conductor.core.db import create_engine
    from conductor.seed import seed

    async def main() -> None:
        engine = create_engine(settings)
        try:
            await seed(engine, dataset_url=settings.demo_dataset_url)
        finally:
            await engine.dispose()

    asyncio.run(main())


_COMMANDS: dict[str, Callable[[Settings, argparse.Namespace], None]] = {
    "api": _run_api,
    "worker": _run_worker,
    "scheduler": _run_scheduler,
    "migrate": _run_migrate,
    "seed": _run_seed,
}


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and run the selected process type."""
    parser = argparse.ArgumentParser(prog="conductor", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("api", help="Serve the HTTP API.")
    worker = sub.add_parser("worker", help="Claim and execute tasks.")
    worker.add_argument("--concurrency", type=int, help="Task slots (default from settings).")
    sub.add_parser(
        "scheduler", help="Run the reaper (and, in later phases, cron and run advancement)."
    )
    sub.add_parser("migrate", help="Apply database migrations up to head.")
    sub.add_parser("seed", help="Create the sample workflow if it does not exist.")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    _COMMANDS[args.command](settings, args)


if __name__ == "__main__":
    main()
