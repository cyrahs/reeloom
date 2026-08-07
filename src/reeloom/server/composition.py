from __future__ import annotations

import hmac
import logging
import os
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath

from fastapi import FastAPI

from reeloom.adapters.journal import FilesystemJournalStore
from reeloom.adapters.forward_filesystem import PosixForwardFilesystem
from reeloom.adapters.folder_journal import FilesystemFolderJournalStore
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.adapters.telegram import TelegramHttpAdapter
from reeloom.adapters.acgrip import (
    AcgripSubtitleArchiveFetcher,
    AcgripSubtitleSearchProvider,
)
from reeloom.adapters.approval import FilesystemApprovalStore
from reeloom.adapters.subtitle_archive import (
    FilesystemSubtitleArchiveInspector,
)
from reeloom.adapters.subtitle_archive_cache import (
    FilesystemSubtitleArchiveCache,
)
from reeloom.adapters.subtitle_plan_store import (
    FilesystemSubtitleAcquisitionPlanStore,
)
from reeloom.executor.apply import FilesystemExecutor
from reeloom.executor.forward import ForwardExecutor
from reeloom.executor.folder_disposition import FolderDispositionExecutor
from reeloom.executor.subtitle_marker_acquisition import (
    SubtitleMarkerAcquisitionExecutor,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.api import ApiDependencies, create_api
from reeloom.server.agent_repository import (
    PostgresAgentDefinitionRepository,
)
from reeloom.server.agent_worker import (
    InitialAgentWorker,
    ModelLeaseFactory,
    TmdbLeaseFactory,
)
from reeloom.server.subtitle_acquisition import SubtitleAcquisitionPlanner
from reeloom.server.subtitle_acquisition_service import (
    SubtitleAcquisitionCoordinator,
)
from reeloom.server.subtitle_successor import (
    SubtitleFreshScan,
    SubtitleFreshScanError,
    SubtitleSuccessorClaim,
    SubtitleSuccessorWorker,
)
from reeloom.server.subtitle_successor_repository import (
    PostgresSubtitleSuccessorOutbox,
)
from reeloom.server.subtitle_publication_repository import (
    PostgresSubtitlePublicationRepository,
)
from reeloom.server.subtitle_scan import SubtitleScanWorker
from reeloom.server.watcher import FolderSnapshot, NoFollowWatcher
from reeloom.server.apply_service import ApplyCoordinator
from reeloom.server.archive_directory import run_directory_io
from reeloom.server.approval_repository import PostgresApprovalStore
from reeloom.server.auth import AuthSettings
from reeloom.server.background import BackgroundServices
from reeloom.server.completed_layout import (
    PostgresCompletedLayoutRepository,
)
from reeloom.server.folder_disposition import (
    FolderDispositionCoordinator,
    FolderDispositionPlanner,
    PostgresFolderDispositionRepository,
)
from reeloom.server.forward_execution_service import (
    ForwardExecutionCoordinator,
)
from reeloom.server.forward_operation_repository import (
    PostgresForwardOperationRepository,
)
from reeloom.server.forward_rescan import ForwardRescanWorker
from reeloom.server.database import PostgresControlPlane
from reeloom.server.directory_browser import PodDirectoryBrowser
from reeloom.server.instance_lock import ProcessLock
from reeloom.server.interaction_repository import (
    PostgresInteractionRepository,
)
from reeloom.server.interaction_executor import AgentInteractionExecutor
from reeloom.server.interactions import (
    InteractionExecution,
    InteractionRequest,
    InteractionService,
)
from reeloom.ports.archive_directory import ArchiveDirectoryError
from reeloom.server.idempotency import PostgresIdempotencyService
from reeloom.server.queries import PostgresQueries
from reeloom.server.move_capability import (
    MoveCapability,
    MoveCapabilityStatus,
    probe_move_capability,
)
from reeloom.server.notification_delivery import (
    ConfiguredNotificationDelivery,
    SenderFactory,
    TelegramTestQueue,
)
from reeloom.server.notification_outbox import PostgresNotificationOutbox
from reeloom.server.notification_projector import (
    PostgresNotificationProjector,
)
from reeloom.server.run_deletion import PostgresRunDeletionService
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.runtime_store import PostgresEventStore
from reeloom.runtime.events import RunFailed
from reeloom.runtime.state import RunStatus
from reeloom.server.organizer_definition import (
    LEGACY_MOVIE_ORGANIZER_SCHEMA_VERSION,
    LEGACY_ORGANIZER_SCHEMA_VERSION,
    PREVIOUS_MOVIE_ORGANIZER_SCHEMA_VERSION,
    PREVIOUS_ORGANIZER_SCHEMA_VERSION,
    V5_ORGANIZER_SCHEMA_VERSION,
    V4_ORGANIZER_SCHEMA_VERSION,
    V3_ORGANIZER_SCHEMA_VERSION,
    V2_MOVIE_ORGANIZER_SCHEMA_VERSION,
    V2_ORGANIZER_SCHEMA_VERSION,
)
from reeloom.server.secrets import FilesystemSecretStore
from reeloom.server.config import (
    ConfigRevision,
    ServerWorkType,
)
from reeloom.server.config_edit import ConfigEdit, parse_config_edit
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.config_service import ConfigService
from reeloom.server.provider import (
    ControlledModelLease,
    ControlledProviderProbe,
)
from reeloom.server.session import PostgresSessionRepository
from reeloom.server.tmdb_provider import TmdbHttpLease
from reeloom.server.settings import DeploymentSettings
from reeloom.server.errors import ServerError, ServerErrorCode

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApplicationHealth:
    postgres_major: int
    schema_version: int
    notification_pending: int
    notification_dead: int
    telegram_configured: bool


@dataclass(frozen=True, slots=True)
class _AcgripSearchLease:
    provider: AcgripSubtitleSearchProvider

    async def close(self) -> None:
        await self.provider.aclose()


@dataclass(frozen=True, slots=True)
class _AcgripPlanningLease:
    fetcher: AcgripSubtitleArchiveFetcher
    planner: SubtitleAcquisitionPlanner

    async def close(self) -> None:
        await self.fetcher.aclose()


@dataclass(frozen=True, slots=True)
class _AcgripExecutorLease:
    fetcher: AcgripSubtitleArchiveFetcher
    executor: SubtitleMarkerAcquisitionExecutor

    async def close(self) -> None:
        await self.fetcher.aclose()


@dataclass(frozen=True, slots=True)
class _ConfiguredSubtitleFreshScanner:
    configs: PostgresConfigRepository
    watcher: NoFollowWatcher = NoFollowWatcher()

    def scan(self, claim: SubtitleSuccessorClaim) -> SubtitleFreshScan:
        try:
            config = self.configs.get(claim.config_revision)
            watch = next(
                (
                    item
                    for item in config.watches
                    if item.watch_id == claim.watch_id
                    and item.work_type is ServerWorkType.ANIME
                ),
                None,
            )
            if watch is None:
                raise SubtitleFreshScanError(retryable=False)
            return SubtitleFreshScan(
                snapshot=self.watcher.scan_folder(
                    AuthorizedRoot.create(watch.root),
                    PurePosixPath(claim.settlement.source_folder),
                    logical_name=claim.settlement.source_folder,
                ),
                settle_for=timedelta(
                    seconds=watch.settle_interval_seconds
                ),
            )
        except SubtitleFreshScanError:
            raise
        except ServerError as error:
            raise SubtitleFreshScanError(
                retryable=(
                    error.code is ServerErrorCode.DATABASE_UNAVAILABLE
                )
            ) from None
        except Exception:
            raise SubtitleFreshScanError(retryable=True) from None


def _state_subdirectory(root: Path, name: str) -> AuthorizedRoot:
    root_fd = -1
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o022
        ):
            raise ServerError(ServerErrorCode.UNSAFE_STATE_ROOT)
    except ServerError:
        raise
    except OSError:
        raise ServerError(ServerErrorCode.UNSAFE_STATE_ROOT) from None
    finally:
        if root_fd >= 0:
            os.close(root_fd)
    return AuthorizedRoot.create(root / name)


@dataclass(slots=True)
class ServerApplication:
    settings: DeploymentSettings
    boot_id: str
    process_lock: ProcessLock
    database: PostgresControlPlane
    api: FastAPI
    background: BackgroundServices
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        errors: list[BaseException] = []
        for action in (
            self.background.close,
            lambda: self.database.stop_boot(self.boot_id),
            self.database.close,
            self.process_lock.close,
        ):
            try:
                action()
            except BaseException as error:
                errors.append(error)
        self._closed = True
        if errors:
            for error in errors[1:]:
                errors[0].add_note(
                    f"additional cleanup failure: {type(error).__name__}"
                )
            raise errors[0]


def _retire_unplanned_folder_runs(
    *,
    database: PostgresControlPlane,
    plans: FilesystemPlanStore,
    scheduler: PostgresSchedulerRepository,
) -> None:
    retired = (
        (
            (
                LEGACY_ORGANIZER_SCHEMA_VERSION,
                LEGACY_MOVIE_ORGANIZER_SCHEMA_VERSION,
            ),
            "retired_tool_call",
            tuple(ServerWorkType),
        ),
        (
            (
                V2_ORGANIZER_SCHEMA_VERSION,
                V2_MOVIE_ORGANIZER_SCHEMA_VERSION,
            ),
            "retired_agent_definition",
            tuple(ServerWorkType),
        ),
        (
            (
                V3_ORGANIZER_SCHEMA_VERSION,
                PREVIOUS_MOVIE_ORGANIZER_SCHEMA_VERSION,
            ),
            "retired_invalid_tool_schema",
            tuple(ServerWorkType),
        ),
        (
            (V4_ORGANIZER_SCHEMA_VERSION, V5_ORGANIZER_SCHEMA_VERSION),
            "retired_m13_probe_schema",
            (ServerWorkType.ANIME,),
        ),
        (
            (PREVIOUS_ORGANIZER_SCHEMA_VERSION,),
            "retired_m13_agent_loop_schema",
            (ServerWorkType.ANIME,),
        ),
    )
    for schema_versions, failure_code, work_types in retired:
        for run_id in scheduler.retired_unplanned_folder_runs(
            schema_versions=schema_versions,
            work_types=work_types,
        ):
            event_store = PostgresEventStore(
                database.pool,
                run_id=run_id,
                plans=plans,
            )
            if (
                event_store.state is not None
                and event_store.state.status is not RunStatus.FAILED
            ):
                event_store.append(RunFailed(code=failure_code))
            scheduler.restart_folder_generation(
                run_id=run_id,
                audit_event=failure_code,
            )
            _LOG.warning(
                "retired_folder_agent_definition run_id=%s "
                "failure_code=%s",
                run_id,
                failure_code,
            )


def build_application(
    settings: DeploymentSettings,
    *,
    auth: AuthSettings,
    interaction_execute: (
        Callable[[InteractionRequest], InteractionExecution] | None
    ) = None,
    model_factory: ModelLeaseFactory | None = None,
    tmdb_factory: TmdbLeaseFactory | None = None,
    telegram_factory: SenderFactory | None = None,
) -> ServerApplication:
    if settings.workers != 1:
        raise ServerError(ServerErrorCode.MULTIPLE_WORKERS)
    AuthorizedRoot.create(settings.state_root)
    process_lock = ProcessLock.acquire(settings.state_root)
    database = PostgresControlPlane(settings.postgres_dsn)
    try:
        database.open()
        database.migrate()
        database.health()
        database.acquire_instance_lock()
        boot_id = f"boot-{uuid.uuid4().hex}"
        database.register_boot(boot_id)

        secret_root = _state_subdirectory(
            settings.state_root, "secrets"
        )
        plan_root = _state_subdirectory(settings.state_root, "plans")
        journal_root = _state_subdirectory(
            settings.state_root, "journals"
        )
        folder_journal_root = _state_subdirectory(
            settings.state_root, "folder-journals"
        )
        subtitle_plan_root = _state_subdirectory(
            settings.state_root, "subtitle-plans"
        )
        subtitle_approval_root = _state_subdirectory(
            settings.state_root, "subtitle-approvals"
        )
        subtitle_workspace = _state_subdirectory(
            settings.state_root, "subtitle-workspace"
        )
        subtitle_archive_cache_root = _state_subdirectory(
            settings.state_root, "subtitle-archive-cache"
        )
        secrets = FilesystemSecretStore(secret_root)
        plans = FilesystemPlanStore(plan_root)
        journals = FilesystemJournalStore(journal_root)
        approvals = PostgresApprovalStore(database.pool)
        forward_operations = PostgresForwardOperationRepository(database.pool)
        notification_outbox = PostgresNotificationOutbox(database.pool)
        notification_projector = PostgresNotificationProjector(
            plans=plans,
            outbox=notification_outbox,
        )
        layouts = PostgresCompletedLayoutRepository(
            database.pool,
            notifications=notification_projector,
        )
        executor = FilesystemExecutor(
            plans=plans,
            approvals=approvals,
            journals=journals,
        )
        apply = ApplyCoordinator(
            pool=database.pool,
            approvals=approvals,
            executor=executor,
            completed_layouts=layouts,
        )
        forward_execution = ForwardExecutionCoordinator(
            configs=PostgresConfigRepository(database.pool),
            plans=plans,
            approvals=approvals,
            operations=forward_operations,
            executor=ForwardExecutor(PosixForwardFilesystem()),
            worker_id=boot_id,
        )
        folder_repository = PostgresFolderDispositionRepository(
            database.pool,
            notifications=notification_projector,
        )
        folder_planner = FolderDispositionPlanner(
            pool=database.pool,
            plans=plans,
            repository=folder_repository,
        )
        folder_dispositions = FolderDispositionCoordinator(
            pool=database.pool,
            plans=plans,
            repository=folder_repository,
            planner=folder_planner,
            executor=FolderDispositionExecutor(
                plans=plans,
                approvals=folder_repository,
                journals=FilesystemFolderJournalStore(
                    folder_journal_root
                ),
            ),
        )
        apply.reconcile_active()
        interactions_repository = PostgresInteractionRepository(
            database.pool,
            notifications=notification_projector,
        )
        interactions_repository.reconcile_active()
        idempotency = PostgresIdempotencyService(database.pool)
        idempotency.reconcile_active()
        run_deletions = PostgresRunDeletionService(database.pool)
        scheduler = PostgresSchedulerRepository(database.pool)
        scheduler.reconcile_boot(current_boot_id=boot_id)
        _retire_unplanned_folder_runs(
            database=database,
            plans=plans,
            scheduler=scheduler,
        )
        config_repository = PostgresConfigRepository(database.pool)
        subtitle_plans = FilesystemSubtitleAcquisitionPlanStore(
            subtitle_plan_root
        )
        subtitle_approvals = FilesystemApprovalStore(
            subtitle_approval_root
        )
        subtitle_inspector = FilesystemSubtitleArchiveInspector()
        subtitle_archive_cache = FilesystemSubtitleArchiveCache(
            subtitle_archive_cache_root
        )
        subtitle_successor_outbox = PostgresSubtitleSuccessorOutbox(
            database.pool
        )
        subtitle_publications = PostgresSubtitlePublicationRepository(
            database.pool
        )

        def subtitle_planning_factory() -> _AcgripPlanningLease:
            fetcher = AcgripSubtitleArchiveFetcher(subtitle_workspace)
            return _AcgripPlanningLease(
                fetcher,
                SubtitleAcquisitionPlanner(
                    fetcher,
                    subtitle_inspector,
                    subtitle_plans,
                    subtitle_archive_cache,
                ),
            )

        def subtitle_executor_factory() -> _AcgripExecutorLease:
            fetcher = AcgripSubtitleArchiveFetcher(subtitle_workspace)
            return _AcgripExecutorLease(
                fetcher,
                SubtitleMarkerAcquisitionExecutor(
                    subtitle_plans,
                    subtitle_approvals,
                    subtitle_archive_cache,
                    fetcher,
                    subtitle_inspector,
                ),
            )

        subtitle_acquisitions = SubtitleAcquisitionCoordinator(
            pool=database.pool,
            plans=subtitle_plans,
            approvals=subtitle_approvals,
            executor_factory=subtitle_executor_factory,
            successors=subtitle_successor_outbox,
            publications=subtitle_publications,
        )
        subtitle_acquisitions.reconcile_approved()
        subtitle_successor_worker = SubtitleSuccessorWorker(
            outbox=subtitle_successor_outbox,
            scanner=_ConfiguredSubtitleFreshScanner(config_repository),
        )
        config_service = ConfigService(
            configs=config_repository,
            secrets=secrets,
        )
        telegram_tests = TelegramTestQueue(
            configs=config_repository,
            outbox=notification_outbox,
        )

        def parse_config(
            expected_revision: int,
            value: dict[str, object],
        ) -> ConfigEdit:
            current: ConfigRevision | None = None
            if expected_revision > 0:
                current = config_repository.get(expected_revision)
            return parse_config_edit(value, current=current)

        def update_config(
            expected_revision: int,
            value: dict[str, object],
        ) -> dict[str, object]:
            edit = parse_config(expected_revision, value)
            revision = config_service.compare_and_append_draft(
                expected_revision=expected_revision,
                draft=edit.draft,
                replacement_api_key=edit.replacement_api_key,
                replacement_telegram_token=(
                    edit.replacement_telegram_token
                ),
            )
            return revision.public_payload()

        def resolve_config(
            expected_revision: int,
            value: dict[str, object],
        ) -> dict[str, object] | None:
            expected = parse_config(expected_revision, value)
            try:
                revision = config_repository.get(
                    expected_revision + 1
                )
            except ServerError as error:
                if error.code is ServerErrorCode.CONFIG_NOT_FOUND:
                    return None
                raise
            provider = revision.provider
            telegram = revision.telegram
            if (
                revision.watches != expected.draft.watches
                or revision.apply_policy is not expected.draft.apply_policy
                or provider.base_url
                != expected.draft.provider.base_url
                or provider.model != expected.draft.provider.model
                or provider.reasoning_effort
                != expected.draft.provider.reasoning_effort
                or provider.verbosity
                != expected.draft.provider.verbosity
                or (
                    expected.replacement_api_key is None
                    and provider.secret_ref
                    != expected.draft.provider.secret_ref
                )
                or (
                    expected.replacement_api_key is not None
                    and not hmac.compare_digest(
                        secrets.load(provider.secret_ref),
                        expected.replacement_api_key,
                    )
                )
                or telegram.enabled != expected.draft.telegram.enabled
                or revision.acgrip != expected.draft.acgrip
                or revision.subtitle_acquisition_policy
                is not expected.draft.subtitle_acquisition_policy
                or telegram.notification_types
                != expected.draft.telegram.notification_types
                or telegram.chat_id != expected.draft.telegram.chat_id
                or (
                    expected.replacement_telegram_token is None
                    and telegram.secret_ref
                    != expected.draft.telegram.secret_ref
                )
                or (
                    expected.replacement_telegram_token is not None
                    and not hmac.compare_digest(
                        secrets.load(telegram.secret_ref),
                        expected.replacement_telegram_token,
                    )
                )
            ):
                return None
            return revision.public_payload()

        controlled_probe = ControlledProviderProbe()
        effective_model_factory = (
            model_factory
            if model_factory is not None
            else lambda config, secret: ControlledModelLease(
                config=config.provider,
                api_key=secret,
            )
        )
        effective_tmdb_factory = (
            tmdb_factory
            if tmdb_factory is not None
            else lambda: TmdbHttpLease(settings.tmdb_api_key)
        )
        effective_telegram_factory = (
            telegram_factory
            if telegram_factory is not None
            else lambda token, chat_id: TelegramHttpAdapter(
                bot_token=token,
                chat_id=chat_id,
            )
        )
        session_repository = PostgresSessionRepository(database.pool)
        definition_repository = PostgresAgentDefinitionRepository(
            database.pool
        )

        async def probe_provider() -> object:
            config = config_repository.head()
            if config is None:
                raise ServerError(ServerErrorCode.CONFIG_NOT_FOUND)
            return await controlled_probe.probe(
                config=config.provider,
                api_key=secrets.load(config.provider.secret_ref),
            )

        async def probe_moves(watch_id: str) -> dict[str, object]:
            config = config_repository.head()
            if config is None:
                raise ServerError(ServerErrorCode.CONFIG_NOT_FOUND)
            watch = next(
                (
                    item
                    for item in config.watches
                    if item.watch_id == watch_id
                ),
                None,
            )
            if watch is None:
                raise ServerError(ServerErrorCode.WATCH_NOT_FOUND)

            def execute() -> tuple[MoveCapability, MoveCapability]:
                source = AuthorizedRoot.create(watch.root)
                return (
                    probe_move_capability(source, source),
                    probe_move_capability(
                        source,
                        AuthorizedRoot.create(watch.library_root),
                    ),
                )

            try:
                folder, media = await run_directory_io(
                    execute,
                    timeout_seconds=60.0,
                )
            except ArchiveDirectoryError as error:
                uncertain = MoveCapability(
                    MoveCapabilityStatus.UNCERTAIN,
                    error.code,
                )
                folder = media = uncertain
            return {
                "watch_id": watch.watch_id,
                "move_backend": (
                    "fuse_checked_rename"
                    if any(
                        item.move_backend.value
                        == "fuse_checked_rename"
                        for item in (folder, media)
                    )
                    else "native"
                ),
                "folder_disposition": folder.payload(),
                "media_apply": media.payload(),
            }
        effective_interaction_execute = (
            interaction_execute
            if interaction_execute is not None
            else AgentInteractionExecutor(
                scheduler=scheduler,
                definitions=definition_repository,
                configs=config_repository,
                sessions=session_repository,
                layouts=layouts,
                secrets=secrets,
                plans=plans,
                model_factory=effective_model_factory,
                tmdb_factory=effective_tmdb_factory,
                queries=PostgresQueries(database.pool, plans=plans),
            )
        )
        interactions = InteractionService(
            repository=interactions_repository,
            execute=effective_interaction_execute,
        )
        worker = InitialAgentWorker(
            scheduler=scheduler,
            configs=config_repository,
            definitions=definition_repository,
            sessions=session_repository,
            secrets=secrets,
            plans=plans,
            model_factory=effective_model_factory,
            tmdb_factory=effective_tmdb_factory,
            pool=database.pool,
            notifications=notification_projector,
            subtitle_search_factory=lambda: _AcgripSearchLease(
                AcgripSubtitleSearchProvider()
            ),
            subtitle_planning_factory=subtitle_planning_factory,
            subtitle_plan_sink=subtitle_acquisitions.register_plan,
            subtitle_lineage_gate=subtitle_publications,
        )
        background = BackgroundServices(
            boot_id=boot_id,
            configs=config_repository,
            scheduler=scheduler,
            worker=worker,
            apply=apply,
            folder_dispositions=folder_dispositions,
            notifications=ConfiguredNotificationDelivery(
                configs=config_repository,
                secrets=secrets,
                outbox=notification_outbox,
                sender_factory=effective_telegram_factory,
                worker_id=boot_id,
            ),
            subtitle_acquisitions=subtitle_acquisitions,
            subtitle_successors=subtitle_successor_worker,
            subtitle_scans=SubtitleScanWorker(
                publications=subtitle_publications,
                scheduler=scheduler,
            ),
            forward_execution=forward_execution,
            forward_rescans=ForwardRescanWorker(
                operations=forward_operations,
                scheduler=scheduler,
            ),
        )

        def health() -> object:
            if background.fatal:
                raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE)
            database_health = database.health()
            notification_stats = notification_outbox.stats()
            config = config_repository.head()
            return ApplicationHealth(
                postgres_major=database_health.postgres_major,
                schema_version=database_health.schema_version,
                notification_pending=notification_stats.pending,
                notification_dead=notification_stats.dead,
                telegram_configured=(
                    config is not None
                    and bool(config.telegram.secret_ref)
                ),
            )

        def retry_attention(
            run_id: str,
            event_sequence: int,
        ) -> dict[str, object]:
            retry_count = scheduler.retry_needs_attention(
                run_id=run_id,
                expected_event_sequence=event_sequence,
            )
            if retry_count is None:
                raise ServerError(
                    ServerErrorCode.INTERACTION_CONFLICT
                )
            return {
                "run_id": run_id,
                "status": "retry_scheduled",
                "retry_count": retry_count,
            }

        def fail_attention(
            run_id: str,
            event_sequence: int,
        ) -> dict[str, object]:
            plan = folder_dispositions.prepare_failure(
                run_id=run_id,
                reason_code="user_marked_failed",
            )
            if plan is None:
                raise ServerError(
                    ServerErrorCode.INTERACTION_CONFLICT
                )
            scheduler.mark_needs_attention_failed(
                run_id=run_id,
                expected_event_sequence=event_sequence,
            )
            return {
                "run_id": run_id,
                "status": "failure_planned",
                "plan_hash": plan.plan_hash,
            }

        api = create_api(
            ApiDependencies(
                queries=PostgresQueries(database.pool, plans=plans),
                interactions=interactions,
                apply=apply,
                forward_execution=forward_execution,
                folder_dispositions=folder_dispositions,
                subtitle_acquisitions=subtitle_acquisitions,
                health=health,
                config_update=update_config,
                config_resolve=resolve_config,
                provider_probe=probe_provider,
                telegram_test=telegram_tests.enqueue,
                move_capability_probe=probe_moves,
                directory_list=PodDirectoryBrowser().list,
                idempotency=idempotency,
                run_delete=run_deletions.delete,
                run_delete_resolve=run_deletions.get,
                attention_retry=retry_attention,
                attention_fail=fail_attention,
                sse_max_empty_polls=None,
                sse_poll_seconds=0.5,
                sse_heartbeat_seconds=15.0,
            ),
            auth=auth,
            static_root=Path(__file__).with_name("static"),
        )
        application = ServerApplication(
            settings=settings,
            boot_id=boot_id,
            process_lock=process_lock,
            database=database,
            api=api,
            background=background,
        )
        background.start()
        return application
    except Exception:
        database.close()
        process_lock.close()
        raise
