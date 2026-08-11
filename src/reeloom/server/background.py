from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agents import MaxTurnsExceeded

from reeloom.executor.apply import ApplyStatus
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.errors import BudgetExceeded, RuntimeDomainError, RuntimeErrorCode
from reeloom.server.agent_worker import (
    AgentWorkKind,
    AgentWorkResult,
    InitialAgentWorker,
)
from reeloom.server.apply_service import ApplyCoordinator
from reeloom.server.config import (
    ApplyPolicy,
    ConfigRevision,
    ServerWorkType,
    SubtitleProvider,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.job_outcome import AgentWorkFailure
from reeloom.server.folder_disposition import FolderDispositionCoordinator
from reeloom.server.forward_execution_service import (
    ForwardExecutionCoordinator,
)
from reeloom.server.forward_rescan import ForwardRescanWorker
from reeloom.server.folder_housekeeping_v2 import FolderHousekeepingWorker
from reeloom.server.notification_delivery import (
    ConfiguredNotificationDelivery,
)
from reeloom.server.notification_intents import (
    PostgresNotificationIntentWorker,
)
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.watcher import NoFollowWatcher
from reeloom.server.subtitle_acquisition_service import (
    SubtitleAcquisitionCoordinator,
)

_LOG = logging.getLogger(__name__)
_MAX_FOLDER_FAILURE_RETRIES = 3
_SUBTITLE_RECONCILE_INTERVAL_SECONDS = 30.0


def _semantic_watch_v2_enabled(
    config: ConfigRevision,
    work_type: ServerWorkType,
) -> bool:
    del config
    return work_type in {
        ServerWorkType.ANIME,
        ServerWorkType.TV,
        ServerWorkType.MOVIE,
    }


@dataclass(slots=True)
class BackgroundServices:
    """Single-process poller and job worker with no database-spanning I/O."""

    boot_id: str
    configs: PostgresConfigRepository
    scheduler: PostgresSchedulerRepository
    worker: InitialAgentWorker
    instance_guard: Callable[[], None] | None = None
    apply: ApplyCoordinator | None = None
    folder_dispositions: FolderDispositionCoordinator | None = None
    notifications: ConfiguredNotificationDelivery | None = None
    notification_intents: PostgresNotificationIntentWorker | None = None
    subtitle_acquisitions: SubtitleAcquisitionCoordinator | None = None
    forward_execution: ForwardExecutionCoordinator | None = None
    forward_rescans: ForwardRescanWorker | None = None
    folder_housekeeping_v2: FolderHousekeepingWorker | None = None
    legacy_effects_enabled: bool = False
    watcher: NoFollowWatcher = NoFollowWatcher()
    idle_seconds: float = 0.25
    _stop: threading.Event = field(
        init=False,
        default_factory=threading.Event,
    )
    _thread: threading.Thread | None = field(init=False, default=None)
    _next_poll: dict[str, float] = field(init=False, default_factory=dict)
    _configured_revision: int | None = field(init=False, default=None)
    _next_subtitle_reconcile: float = field(init=False, default=0.0)
    _fatal: threading.Event = field(
        init=False,
        default_factory=threading.Event,
    )

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("background services already started")
        if self.notifications is not None:
            self.notifications.start()
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
            if self.notifications is not None:
                self.notifications.close()
            return
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise RuntimeError("background services did not stop")
        self._thread = None
        if self.notifications is not None:
            self.notifications.close()

    @property
    def fatal(self) -> bool:
        return self._fatal.is_set()

    def _run(self) -> None:
        while not self._stop.is_set():
            progressed = False
            try:
                if self.instance_guard is not None:
                    self.instance_guard()
                config = self.configs.head()
                if config is not None:
                    self._configure(config)
                    progressed = self._poll_due(config) or progressed
                if (
                    self.subtitle_acquisitions is not None
                    and time.monotonic() >= self._next_subtitle_reconcile
                ):
                    self._next_subtitle_reconcile = (
                        time.monotonic()
                        + _SUBTITLE_RECONCILE_INTERVAL_SECONDS
                    )
                    progressed = bool(
                        self.subtitle_acquisitions.reconcile_approved()
                    ) or progressed
                claimed = self.scheduler.claim_job(boot_id=self.boot_id)
                if claimed is not None:
                    progressed = True
                    self._execute_job(claimed.job_id, claimed.run_id)
                if self.notifications is not None:
                    if self.notification_intents is not None:
                        progressed = (
                            self.notification_intents.process_one()
                            or progressed
                        )
                    progressed = self.notifications.run_once() or progressed
                if self.forward_execution is not None:
                    progressed = (
                        self.forward_execution.reconcile_one() is not None
                    ) or progressed
                if self.forward_rescans is not None:
                    progressed = self.forward_rescans.process_one(
                        worker_id=self.boot_id,
                        now=datetime.now(UTC),
                    ) or progressed
                if self.folder_housekeeping_v2 is not None:
                    progressed = self.folder_housekeeping_v2.process_one(
                        worker_id=self.boot_id,
                        now=datetime.now(UTC),
                    ) or progressed
            except Exception as error:
                _LOG.error(
                    "background_cycle_failed error_type=%s",
                    type(error).__name__,
                )
                if (
                    isinstance(error, ServerError)
                    and error.code
                    in {
                        ServerErrorCode.DATABASE_UNAVAILABLE,
                        ServerErrorCode.INSTANCE_LOCK_LOST,
                    }
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
                semantic_v2=_semantic_watch_v2_enabled(
                    config, watch.work_type
                ),
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
        subtitle_work = False
        job_already_settled = False
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
            if hasattr(self.worker, "run_result"):
                work = asyncio.run(self.worker.run_result(run_id=run_id))
            else:
                work = AgentWorkResult(
                    AgentWorkKind.MEDIA_PLAN,
                    asyncio.run(self.worker.run(run_id=run_id)),
                )
            if work.kind is AgentWorkKind.NEEDS_ATTENTION:
                succeeded = True
                return
            if work.plan_hash is None:
                raise RuntimeError("agent work omitted plan hash")
            plan_hash = work.plan_hash
            subtitle_work = (
                work.kind is AgentWorkKind.SUBTITLE_ACQUISITION
            )
            if subtitle_work:
                # M14.6 plan handoff owns the planning-job terminal write.
                job_already_settled = (
                    getattr(self.worker, "run_controls", None) is not None
                )
                if self.subtitle_acquisitions is None:
                    raise RuntimeError(
                        "subtitle acquisition service unavailable"
                    )
                config = self.configs.get(
                    context.registration.config_revision
                )
                watch = next(
                    (
                        item
                        for item in config.watches
                        if item.watch_id == context.discovery.watch_id
                    ),
                    None,
                )
                if (
                    watch is None
                    or not watch.subtitle_acquisition.enabled
                    or watch.subtitle_acquisition.provider
                    is not SubtitleProvider.ACGRIP
                ):
                    raise RuntimeError("subtitle watch unavailable")
                if (
                    watch.subtitle_acquisition.policy.value
                    == "automatic"
                ):
                    self.subtitle_acquisitions.approve_and_execute(
                        run_id=run_id,
                        plan_hash=plan_hash,
                        automatic=True,
                    )
                    # The first execution attempt belongs to the same
                    # automatic job.  The periodic reconciler is only the
                    # crash/restart safety net; waiting for its 30-second
                    # cadence here makes a healthy operation look stuck in
                    # ``authorized`` immediately after Agent completion.
                    self.subtitle_acquisitions.reconcile_approved()
                    # Both the v1 coordinator and the v2 control handoff own
                    # their planning-job settlement in automatic mode.
                    job_already_settled = True
                succeeded = True
                return
            config = self.configs.get(
                context.registration.config_revision
            )
            forward_v2 = (
                self.forward_execution is not None
                and self.forward_execution.is_v2_plan(
                    run_id=run_id,
                    plan_hash=plan_hash,
                )
            )
            if forward_v2:
                # The canonical effect head was committed by the Agent worker;
                # effect execution now has an independent durable lease.
                job_already_settled = (
                    getattr(self.worker, "run_controls", None) is not None
                )
                if config.apply_policy is ApplyPolicy.AUTOMATIC:
                    self.forward_execution.execute_automatic(
                        run_id=run_id,
                        plan_hash=plan_hash,
                    )
                succeeded = True
                return
            if not self.legacy_effects_enabled or self.apply is None:
                raise RuntimeError("legacy_effect_superseded")
            disposition = (
                None
                if (
                    self.folder_dispositions is None
                    or config.apply_policy is ApplyPolicy.PLAN_ONLY
                )
                else self.folder_dispositions.prepare_success(
                    run_id=run_id,
                    media_plan_hash=plan_hash,
                )
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
            work_failure = (
                error.failure
                if isinstance(error, AgentWorkFailure)
                else None
            )
            if work_failure is not None:
                subtitle_work = (
                    work_failure.stage.value == "subtitle_plan"
                )
            if (
                isinstance(error, ServerError)
                and error.code is ServerErrorCode.DATABASE_UNAVAILABLE
            ):
                database_error = error
            reason_code = (
                None if subtitle_work else self._failure_reason(error)
            )
            if (
                subtitle_work
                and work_failure is None
                and database_error is None
                and self.subtitle_acquisitions is not None
            ):
                request = self.subtitle_acquisitions.resolve(
                    run_id=run_id,
                    plan_hash=plan_hash,
                )
                if request is not None and request.status == "blocked":
                    succeeded = True
                elif request is not None and request.status == "published":
                    succeeded = True
                else:
                    # Planning or execution may have persisted durable state
                    # before the transient failure. Retry this same job so the
                    # acquisition service can recover it; never send subtitle
                    # work through the media-plan retry path.
                    retry = True
            execution_blocked = (
                not subtitle_work
                and isinstance(error, ExecutorError)
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
            if subtitle_work:
                if work_failure is not None and database_error is None:
                    self._terminalize_preserving_source(
                        run_id=run_id,
                        reason_code=work_failure.code,
                    )
                    succeeded = True
            elif execution_blocked:
                self._terminalize_preserving_source(
                    run_id=run_id,
                    reason_code=(
                        error.code.value
                        if isinstance(error, ExecutorError)
                        else "execution_failed"
                    ),
                )
                succeeded = True
            elif reason_code is not None:
                try:
                    self._terminalize_preserving_source(
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
            elif folder_run and database_error is None:
                try:
                    self._terminalize_preserving_source(
                        run_id=run_id,
                        reason_code="internal_error",
                    )
                    succeeded = True
                except ServerError as retry_error:
                    if (
                        retry_error.code
                        is ServerErrorCode.DATABASE_UNAVAILABLE
                    ):
                        database_error = retry_error
                    else:
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
                                retry = True
                except Exception:
                    succeeded = False
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
                        retry = False
            _LOG.exception(
                "agent_job_failed run_id=%s error_type=%s error_code=%s "
                "error_context=%s",
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
                (
                    dict(error.context)
                    if isinstance(error, ExecutorError)
                    else {}
                ),
            )
        finally:
            if database_error is not None:
                # Leave the job owned by this boot. Startup reconciliation will
                # return it to pending after database connectivity recovers.
                pass
            elif job_already_settled:
                pass
            elif restarted:
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
        if (
            self.folder_housekeeping_v2 is not None
            and self.folder_housekeeping_v2.enqueue_failure(
                run_id=run_id,
                reason_code=reason_code,
            )
        ):
            return
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

    def _terminalize_preserving_source(
        self, *, run_id: str, reason_code: str
    ) -> None:
        terminalize = getattr(
            self.scheduler, "terminalize_run_failure", None
        )
        if terminalize is not None:
            terminalize(run_id=run_id, failure_code=reason_code)
            return
        self.scheduler.mark_run_failed(run_id=run_id)

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
