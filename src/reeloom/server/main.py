"""Process entry point: one API server with the worker alongside it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from reeloom.config import Settings
from reeloom.db import Database
from reeloom.executor import FilesystemExecutor
from reeloom.agent.identify import AgentIdentifier
from reeloom.server.api import create_app
from reeloom.server.composition import Answerer, Clients
from reeloom.server.worker import Worker

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def build_subtitles(database: Database, clients: Clients, settings: Settings):
    from reeloom.server.subtitles import SubtitleAcquisition

    return SubtitleAcquisition(database, clients, settings.work_dir)


def build_notifier(clients: Clients, settings: Settings):
    from reeloom.server.notify import TelegramNotifier

    return TelegramNotifier(clients, public_url=settings.public_url)


def build_comparer(database: Database, clients: Clients):
    from reeloom.server.compare import ReplaceComparer

    return ReplaceComparer(database, clients)


def build(settings: Settings, database: Database):
    clients = Clients(database)
    worker = Worker(
        database,
        identifier=AgentIdentifier(clients, database),
        executor=FilesystemExecutor(database),
        comparer=build_comparer(database, clients),
        subtitles=build_subtitles(database, clients, settings),
        notifier=build_notifier(clients, settings),
        scan_interval_seconds=settings.scan_interval_seconds,
    )
    app = create_app(
        database=database,
        admin_token=settings.admin_token,
        worker=worker,
        answerer=Answerer(clients),
        static_dir=STATIC_DIR,
    )
    return app, worker, clients


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.from_env()
    settings.work_dir.mkdir(parents=True, exist_ok=True)

    async def serve() -> None:
        async with Database.connect(settings.database_url) as database:
            app, worker, clients = build(settings, database)

            @asynccontextmanager
            async def lifespan(_):
                task = asyncio.create_task(worker.run_forever())
                try:
                    yield
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    await clients.aclose()

            app.router.lifespan_context = lifespan
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=settings.host,
                    port=settings.port,
                    log_level="info",
                    access_log=False,
                )
            )
            await server.serve()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
