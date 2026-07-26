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
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.executor.apply import FilesystemExecutor
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
from reeloom.server.database import PostgresControlPlane
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
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.secrets import FilesystemSecretStore
from reeloom.server.config import (
    ApplyPolicy,
    ArchiveRoute,
    ConfigDraftInput,
    ProviderConfigInput,
    ServerWorkType,
    WatchConfig,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.config_service import ConfigService
from reeloom.server.provider import (
    ControlledModelLease,
    ControlledProviderProbe,
    ProviderOriginPolicy,
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
    database = PostgresControlPlane(
        settings.postgres_dsn,
        migration_dsn=settings.migration_postgres_dsn,
    )
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
        apply.reconcile_active()
        interactions_repository = PostgresInteractionRepository(
            database.pool
        )
        interactions_repository.reconcile_active()
        idempotency = PostgresIdempotencyService(database.pool)
        idempotency.reconcile_active()
        scheduler = PostgresSchedulerRepository(database.pool)
        scheduler.reconcile_boot(current_boot_id=boot_id)
        config_repository = PostgresConfigRepository(database.pool)
        origin_policy = ProviderOriginPolicy.create(
            settings.provider_origins
        )
        config_service = ConfigService(
            configs=config_repository,
            secrets=secrets,
            origins=origin_policy,
        )

        def parse_config(
            value: dict[str, object],
        ) -> ConfigDraftInput:
            try:
                raw_watches = value["watches"]
                raw_routes = value["archive_routes"]
                raw_provider = value["provider"]
                if (
                    not isinstance(raw_watches, list)
                    or not isinstance(raw_routes, list)
                    or not isinstance(raw_provider, dict)
                    or set(raw_provider)
                    != {
                        "api_key",
                        "base_url",
                        "model",
                        "reasoning_effort",
                        "verbosity",
                    }
                ):
                    raise ValueError
                watches = tuple(
                    WatchConfig(
                        watch_id=item["watch_id"],
                        root=Path(item["root"]),
                        work_type=ServerWorkType(item["work_type"]),
                        poll_interval_seconds=item[
                            "poll_interval_seconds"
                        ],
                        settle_interval_seconds=item[
                            "settle_interval_seconds"
                        ],
                    )
                    for item in raw_watches
                    if isinstance(item, dict)
                    and set(item)
                    == {
                        "poll_interval_seconds",
                        "root",
                        "settle_interval_seconds",
                        "watch_id",
                        "work_type",
                    }
                )
                routes = tuple(
                    ArchiveRoute(
                        work_type=ServerWorkType(item["work_type"]),
                        root=Path(item["root"]),
                    )
                    for item in raw_routes
                    if isinstance(item, dict)
                    and set(item) == {"root", "work_type"}
                )
                if len(watches) != len(raw_watches) or len(routes) != len(
                    raw_routes
                ):
                    raise ValueError
                return ConfigDraftInput(
                    watches=watches,
                    archive_routes=routes,
                    provider=ProviderConfigInput(
                        base_url=raw_provider["base_url"],
                        model=raw_provider["model"],
                        api_key=raw_provider["api_key"].encode("utf-8"),
                        reasoning_effort=raw_provider[
                            "reasoning_effort"
                        ],
                        verbosity=raw_provider["verbosity"],
                    ),
                    apply_policy=ApplyPolicy(value["apply_policy"]),
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                raise ServerError(ServerErrorCode.INVALID_CONFIG) from None

        def update_config(
            expected_revision: int,
            value: dict[str, object],
        ) -> dict[str, object]:
            revision = config_service.compare_and_append(
                expected_revision=expected_revision,
                value=parse_config(value),
            )
            return revision.public_payload()

        def resolve_config(
            expected_revision: int,
            value: dict[str, object],
        ) -> dict[str, object] | None:
            expected = parse_config(value)
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
                revision.watches != expected.watches
                or revision.archive_routes != expected.archive_routes
                or revision.apply_policy is not expected.apply_policy
                or provider.base_url != expected.provider.base_url
                or provider.model != expected.provider.model
                or provider.reasoning_effort
                != expected.provider.reasoning_effort
                or provider.verbosity != expected.provider.verbosity
                or not hmac.compare_digest(
                    secrets.load(provider.secret_ref),
                    expected.provider.api_key,
                )
            ):
                return None
            return revision.public_payload()

        controlled_probe = ControlledProviderProbe(
            origins=origin_policy
        )
        effective_model_factory = (
            model_factory
            if model_factory is not None
            else lambda config, secret: ControlledModelLease(
                origins=origin_policy,
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
        )

        def health() -> object:
            if background.fatal:
                raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE)
            return database.health()

        api = create_api(
            ApiDependencies(
                queries=PostgresQueries(database.pool),
                interactions=interactions,
                apply=apply,
                health=health,
                config_update=update_config,
                config_resolve=resolve_config,
                provider_probe=probe_provider,
                idempotency=idempotency,
            ),
            auth=auth,
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
