from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from reeloom.executor.apply import ApplyStatus
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.agent_worker import InitialAgentWorker
from reeloom.server.apply_service import ApplyCoordinator
from reeloom.server.config import ApplyPolicy, ConfigRevision
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.watcher import NoFollowWatcher

_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class BackgroundServices:
    """Single-process poller and job worker with no database-spanning I/O."""

    boot_id: str
    configs: PostgresConfigRepository
    scheduler: PostgresSchedulerRepository
    worker: InitialAgentWorker
    apply: ApplyCoordinator
    watcher: NoFollowWatcher = NoFollowWatcher()
    idle_seconds: float = 0.25
    _stop: threading.Event = field(
        init=False,
        default_factory=threading.Event,
    )
    _thread: threading.Thread | None = field(init=False, default=None)
    _next_poll: dict[str, float] = field(init=False, default_factory=dict)
    _configured_revision: int | None = field(init=False, default=None)
    _fatal: threading.Event = field(
        init=False,
        default_factory=threading.Event,
    )

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("background services already started")
        thread = threading.Thread(
            target=self._run,
            name="reeloom-background",
            daemon=False,
        )
        self._thread = thread
        thread.start()

    def close(self, *, timeout_seconds: float = 70.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise RuntimeError("background services did not stop")
        self._thread = None

    @property
    def fatal(self) -> bool:
        return self._fatal.is_set()

    def _run(self) -> None:
        while not self._stop.is_set():
            progressed = False
            try:
                config = self.configs.head()
                if config is not None:
                    self._configure(config)
                    progressed = self._poll_due(config) or progressed
                claimed = self.scheduler.claim_job(boot_id=self.boot_id)
                if claimed is not None:
                    progressed = True
                    self._execute_job(claimed.job_id, claimed.run_id)
            except Exception as error:
                _LOG.error(
                    "background_cycle_failed error_type=%s",
                    type(error).__name__,
                )
                if (
                    isinstance(error, ServerError)
                    and error.code
                    is ServerErrorCode.DATABASE_UNAVAILABLE
                ):
                    self._fatal.set()
                    self._stop.set()
                    break
            if not progressed:
                self._stop.wait(self.idle_seconds)

    def _configure(self, config: ConfigRevision) -> None:
        if self._configured_revision == config.revision:
            return
        active = {watch.watch_id for watch in config.watches}
        self._next_poll = {
            watch_id: due
            for watch_id, due in self._next_poll.items()
            if watch_id in active
        }
        for watch in config.watches:
            self.scheduler.configure_watch(
                watch_id=watch.watch_id,
                config_revision=config.revision,
                fence=config.revision,
                work_type=watch.work_type,
                settle_interval_seconds=watch.settle_interval_seconds,
            )
            self._next_poll.setdefault(watch.watch_id, 0.0)
        self._configured_revision = config.revision

    def _poll_due(self, config: ConfigRevision) -> bool:
        now = time.monotonic()
        progressed = False
        for watch in config.watches:
            if self._stop.is_set():
                break
            if self._next_poll.get(watch.watch_id, 0.0) > now:
                continue
            progressed = True
            self._next_poll[watch.watch_id] = (
                now + watch.poll_interval_seconds
            )
            try:
                snapshot = self.watcher.scan(
                    AuthorizedRoot.create(watch.root)
                )
                result = self.scheduler.reconcile_poll(
                    watch_id=watch.watch_id,
                    config_revision=config.revision,
                    fence=config.revision,
                    observed_at=datetime.now(UTC),
                    snapshot=snapshot,
                )
                if result.discovery is not None:
                    self.scheduler.register_run(
                        discovery_id=result.discovery.discovery_id
                    )
            except Exception as error:
                if (
                    isinstance(error, ServerError)
                    and error.code
                    is ServerErrorCode.DATABASE_UNAVAILABLE
                ):
                    raise
                _LOG.warning(
                    "watch_poll_failed watch_id=%s error_type=%s",
                    watch.watch_id,
                    type(error).__name__,
                )
        return progressed

    def _execute_job(self, job_id: str, run_id: str) -> None:
        succeeded = False
        database_error: ServerError | None = None
        try:
            plan_hash = asyncio.run(self.worker.run(run_id=run_id))
            context = self.scheduler.get_job_context(run_id=run_id)
            config = self.configs.get(
                context.registration.config_revision
            )
            if config.apply_policy is ApplyPolicy.AUTOMATIC:
                result = self.apply.approve_and_apply(
                    run_id=run_id,
                    plan_hash=plan_hash,
                    automatic=True,
                )
                if result.status is not ApplyStatus.COMPLETED:
                    raise RuntimeError("automatic apply did not complete")
            succeeded = True
        except Exception as error:
            if (
                isinstance(error, ServerError)
                and error.code is ServerErrorCode.DATABASE_UNAVAILABLE
            ):
                database_error = error
            _LOG.error(
                "agent_job_failed run_id=%s error_type=%s",
                run_id,
                type(error).__name__,
            )
        finally:
            self.scheduler.settle_job(
                job_id=job_id,
                boot_id=self.boot_id,
                succeeded=succeeded,
            )
        if database_error is not None:
            raise database_error
