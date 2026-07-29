from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agents import MaxTurnsExceeded

from reeloom.executor.apply import ApplyStatus
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.errors import BudgetExceeded, RuntimeDomainError, RuntimeErrorCode
from reeloom.server.agent_worker import InitialAgentWorker
from reeloom.server.apply_service import ApplyCoordinator
from reeloom.server.config import ApplyPolicy, ConfigRevision
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.folder_disposition import FolderDispositionCoordinator
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
    folder_dispositions: FolderDispositionCoordinator | None = None
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
                scan = self.watcher.scan_folders(
                    AuthorizedRoot.create(watch.root)
                )
                result = self.scheduler.reconcile_folders(
                    watch_id=watch.watch_id,
                    config_revision=config.revision,
                    fence=config.revision,
                    observed_at=datetime.now(UTC),
                    scan=scan,
                )
                for discovery in result.discoveries:
                    self.scheduler.register_run(
                        discovery_id=discovery.discovery_id
                    )
                for run_id in result.disposition_run_ids:
                    self._settle_late_content(run_id=run_id)
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

    def _settle_late_content(self, *, run_id: str) -> None:
        if self.folder_dispositions is None:
            return
        plan = self.folder_dispositions.prepare_current(run_id=run_id)
        if plan is None:
            return
        context = self.scheduler.get_job_context(run_id=run_id)
        config = self.configs.get(context.registration.config_revision)
        if config.apply_policy is ApplyPolicy.AUTOMATIC:
            self.folder_dispositions.approve_and_execute(
                run_id=run_id,
                plan_hash=plan.plan_hash,
                automatic=True,
            )

    def _execute_job(self, job_id: str, run_id: str) -> None:
        succeeded = False
        retry = False
        restarted = False
        folder_run = False
        database_error: ServerError | None = None
        try:
            context = self.scheduler.get_job_context(run_id=run_id)
            discovery = getattr(context, "discovery", None)
            folder_run = (
                discovery is not None
                and discovery.folder_generation_id is not None
            )
            if (
                folder_run
                and not any(
                    item.kind is CandidateKind.VIDEO
                    for item in discovery.snapshot.files
                )
            ):
                self._prepare_terminal_failure(
                    run_id=run_id,
                    reason_code="no_supported_video",
                )
                succeeded = True
                return
            plan_hash = asyncio.run(self.worker.run(run_id=run_id))
            disposition = (
                None
                if self.folder_dispositions is None
                else self.folder_dispositions.prepare_success(
                    run_id=run_id,
                    media_plan_hash=plan_hash,
                )
            )
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
                    raise ExecutorError(
                        result.failure_code
                        or ExecutorErrorCode.RECOVERY_REQUIRED
                    )
                if disposition is not None:
                    try:
                        self.folder_dispositions.approve_and_execute(
                            run_id=run_id,
                            plan_hash=disposition.plan_hash,
                            automatic=True,
                        )
                    except ServerError as error:
                        if (
                            error.code
                            is ServerErrorCode.DATABASE_UNAVAILABLE
                        ):
                            raise
                        _LOG.warning(
                            "folder_disposition_pending "
                            "run_id=%s error_type=%s",
                            run_id,
                            type(error).__name__,
                        )
                    except Exception as error:
                        _LOG.warning(
                            "folder_disposition_pending "
                            "run_id=%s error_type=%s",
                            run_id,
                            type(error).__name__,
                        )
            succeeded = True
        except Exception as error:
            if (
                isinstance(error, ServerError)
                and error.code is ServerErrorCode.DATABASE_UNAVAILABLE
            ):
                database_error = error
            reason_code = self._failure_reason(error)
            execution_blocked = (
                isinstance(error, ExecutorError)
                and error.code
                not in {
                    ExecutorErrorCode.DESTINATION_COLLISION,
                    ExecutorErrorCode.SOURCE_DRIFT,
                }
            )
            restart_generation = (
                isinstance(error, ExecutorError)
                and error.code is ExecutorErrorCode.SOURCE_DRIFT
            )
            if execution_blocked:
                succeeded = True
            elif reason_code is not None:
                try:
                    self._prepare_terminal_failure(
                        run_id=run_id,
                        reason_code=reason_code,
                    )
                    succeeded = True
                except ServerError as disposition_error:
                    if (
                        disposition_error.code
                        is ServerErrorCode.DATABASE_UNAVAILABLE
                    ):
                        database_error = disposition_error
                    else:
                        retry = folder_run
                except ExecutorError:
                    succeeded = True
                except Exception:
                    retry = folder_run
            elif (
                folder_run
                and restart_generation
                and database_error is None
            ):
                try:
                    self.scheduler.restart_folder_generation(
                        run_id=run_id
                    )
                    restarted = True
                except ServerError as restart_error:
                    if (
                        restart_error.code
                        is ServerErrorCode.DATABASE_UNAVAILABLE
                    ):
                        database_error = restart_error
                    else:
                        retry = True
            elif database_error is None:
                try:
                    self.scheduler.mark_run_failed(run_id=run_id)
                    succeeded = True
                except ServerError as failure_error:
                    if (
                        failure_error.code
                        is ServerErrorCode.DATABASE_UNAVAILABLE
                    ):
                        database_error = failure_error
                    elif (
                        failure_error.code
                        is ServerErrorCode.INTERACTION_CONFLICT
                    ):
                        succeeded = True
                    else:
                        retry = folder_run
            _LOG.error(
                "agent_job_failed run_id=%s error_type=%s error_code=%s",
                run_id,
                type(error).__name__,
                (
                    error.code.value
                    if isinstance(
                        error,
                        (
                            DomainError,
                            ExecutorError,
                            RuntimeDomainError,
                            ServerError,
                        ),
                    )
                    else None
                ),
            )
        finally:
            if restarted:
                pass
            elif retry:
                self.scheduler.retry_job(
                    job_id=job_id,
                    boot_id=self.boot_id,
                )
            else:
                self.scheduler.settle_job(
                    job_id=job_id,
                    boot_id=self.boot_id,
                    succeeded=succeeded,
                )
        if database_error is not None:
            raise database_error

    def _prepare_terminal_failure(
        self, *, run_id: str, reason_code: str
    ) -> None:
        if self.folder_dispositions is None:
            raise RuntimeError("folder disposition service unavailable")
        plan = self.folder_dispositions.prepare_failure(
            run_id=run_id,
            reason_code=reason_code,
        )
        if plan is None:
            raise RuntimeError("folder failure disposition unavailable")
        self.scheduler.mark_run_failed(run_id=run_id)
        context = self.scheduler.get_job_context(run_id=run_id)
        config = self.configs.get(context.registration.config_revision)
        if config.apply_policy is ApplyPolicy.AUTOMATIC:
            self.folder_dispositions.approve_and_execute(
                run_id=run_id,
                plan_hash=plan.plan_hash,
                automatic=True,
            )

    @staticmethod
    def _failure_reason(error: Exception) -> str | None:
        if isinstance(error, (MaxTurnsExceeded, BudgetExceeded)):
            return "agent_budget_exhausted"
        if isinstance(error, DomainError) and error.code in {
            ErrorCode.DESTINATION_COLLISION,
            ErrorCode.INVALID_SERIES_TITLE,
            ErrorCode.INVALID_YEAR,
            ErrorCode.INVALID_TMDB_DATA,
            ErrorCode.INVALID_TMDB_ID,
            ErrorCode.INVENTORY_CONFLICT,
        }:
            return f"domain_{error.code.value}"
        if (
            isinstance(error, ExecutorError)
            and error.code is ExecutorErrorCode.DESTINATION_COLLISION
        ):
            return "executor_destination_collision"
        if isinstance(error, RuntimeDomainError) and error.code in {
            RuntimeErrorCode.FAILURE_BUDGET_EXHAUSTED,
            RuntimeErrorCode.TOKEN_BUDGET_EXHAUSTED,
            RuntimeErrorCode.TIME_BUDGET_EXHAUSTED,
            RuntimeErrorCode.SERIES_IDENTITY_UNAVAILABLE,
            RuntimeErrorCode.MOVIE_IDENTITY_UNAVAILABLE,
        }:
            return f"runtime_{error.code.value}"
        return None
