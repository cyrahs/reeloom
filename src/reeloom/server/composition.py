from __future__ import annotations

import hmac
import os
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from reeloom.adapters.journal import FilesystemJournalStore
from reeloom.adapters.folder_journal import FilesystemFolderJournalStore
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.executor.apply import FilesystemExecutor
from reeloom.executor.folder_disposition import FolderDispositionExecutor
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
from reeloom.server.apply_service import ApplyCoordinator
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
from reeloom.server.idempotency import PostgresIdempotencyService
from reeloom.server.queries import PostgresQueries
from reeloom.server.run_deletion import PostgresRunDeletionService
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.secrets import FilesystemSecretStore
from reeloom.server.config import (
    ConfigRevision,
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


def build_application(
    settings: DeploymentSettings,
    *,
    auth: AuthSettings,
    interaction_execute: (
        Callable[[InteractionRequest], InteractionExecution] | None
    ) = None,
    model_factory: ModelLeaseFactory | None = None,
    tmdb_factory: TmdbLeaseFactory | None = None,
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
        secrets = FilesystemSecretStore(secret_root)
        plans = FilesystemPlanStore(plan_root)
        journals = FilesystemJournalStore(journal_root)
        approvals = PostgresApprovalStore(database.pool)
        layouts = PostgresCompletedLayoutRepository(database.pool)
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
        folder_repository = PostgresFolderDispositionRepository(
            database.pool
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
            database.pool
        )
        interactions_repository.reconcile_active()
        idempotency = PostgresIdempotencyService(database.pool)
        idempotency.reconcile_active()
        run_deletions = PostgresRunDeletionService(database.pool)
        scheduler = PostgresSchedulerRepository(database.pool)
        scheduler.reconcile_boot(current_boot_id=boot_id)
        config_repository = PostgresConfigRepository(database.pool)
        config_service = ConfigService(
            configs=config_repository,
            secrets=secrets,
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
        )
        background = BackgroundServices(
            boot_id=boot_id,
            configs=config_repository,
            scheduler=scheduler,
            worker=worker,
            apply=apply,
            folder_dispositions=folder_dispositions,
        )

        def health() -> object:
            if background.fatal:
                raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE)
            return database.health()

        api = create_api(
            ApiDependencies(
                queries=PostgresQueries(database.pool, plans=plans),
                interactions=interactions,
                apply=apply,
                folder_dispositions=folder_dispositions,
                health=health,
                config_update=update_config,
                config_resolve=resolve_config,
                provider_probe=probe_provider,
                directory_list=PodDirectoryBrowser().list,
                idempotency=idempotency,
                run_delete=run_deletions.delete,
                run_delete_resolve=run_deletions.get,
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
