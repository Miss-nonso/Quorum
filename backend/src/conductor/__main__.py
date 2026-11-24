"""Command-line entry point: ``conductor <command>``.

One image, several process types. Phase 0 ships ``api`` and ``migrate``;
``scheduler`` and ``worker`` arrive with the engine in later phases.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from conductor.core.config import get_settings
from conductor.core.logging import configure_logging

# Run from the backend project root (locally) or /app (in the image), where
# alembic.ini and the alembic/ directory live.
_ALEMBIC_INI = Path("alembic.ini")


def _run_api() -> None:
    import uvicorn

    from conductor.api.app import create_app

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # keep our JSON logging configuration
        proxy_headers=True,
    )


def _run_migrate() -> None:
    from alembic import command
    from alembic.config import Config

    if not _ALEMBIC_INI.is_file():
        raise SystemExit(f"{_ALEMBIC_INI.resolve()} not found; run from the backend directory.")
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and run the selected process type."""
    parser = argparse.ArgumentParser(prog="conductor", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("api", help="Serve the HTTP API.")
    sub.add_parser("migrate", help="Apply database migrations up to head.")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    {"api": _run_api, "migrate": _run_migrate}[args.command]()


if __name__ == "__main__":
    main()
