from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agents import MaxTurnsExceeded
from psycopg_pool import ConnectionPool

from reeloom.adapters.filesystem import (
    FilesystemPlanCompiler,
    FilesystemScanResult,
    FilesystemSubtitleSampleProvider,
)
from reeloom.agents.organizer import (
    create_organizer_context,
    run_episode_organizer,
)
from reeloom.kernel.scanner import ScannedFile, build_candidate_snapshot
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.archive_directory import ArchiveDirectoryError
from reeloom.ports.plans import PlanStore
from reeloom.ports.tmdb import TmdbProvider
from reeloom.runtime.errors import BudgetExceeded
from reeloom.runtime.events import ApprovalRequested, RunStopped
from reeloom.runtime.state import Phase, RunStatus, StopReason
from reeloom.server.archive_directory import (
    FilesystemArchiveDirectoryBrowser,
)
from reeloom.server.agent_repository import (
    PostgresAgentDefinitionRepository,
)
from reeloom.server.config import (
    ConfigRevision,
    ServerWorkType,
    WatchConfig,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.organizer_definition import (
    is_supported_organizer_definition,
    organizer_definition,
)
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.provider import ModelLease
from reeloom.server.runtime_store import PostgresEventStore
from reeloom.server.scheduler import AgentJobContext
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.session import (
    PostgresSessionRepository,
    RepositoryAgentSession,
)
from reeloom.server.secrets import FilesystemSecretStore
from reeloom.tools.candidates import SnapshotCandidateSource

_INITIAL_PROMPT = (
    "Inspect the authorized candidate snapshot and submit one complete, "
    "validated mapping for its configured work type."
)


class ModelLeaseFactory(Protocol):
    def __call__(
        self,
        config: ConfigRevision,
        secret: bytes,
    ) -> ModelLease: ...


class TmdbLease(Protocol):
    @property
    def provider(self) -> TmdbProvider: ...

    async def close(self) -> None: ...


class TmdbLeaseFactory(Protocol):
    def __call__(self) -> TmdbLease: ...


@dataclass(frozen=True, slots=True)
class InitialAgentWorker:
    scheduler: PostgresSchedulerRepository
    configs: PostgresConfigRepository
    definitions: PostgresAgentDefinitionRepository
    sessions: PostgresSessionRepository
    secrets: FilesystemSecretStore
    plans: PlanStore
    model_factory: ModelLeaseFactory
    tmdb_factory: TmdbLeaseFactory
    pool: ConnectionPool

    async def run(self, *, run_id: str) -> str:
        job = self.scheduler.get_job_context(run_id=run_id)
        config = self.configs.get(job.registration.config_revision)
        watch, work_type = self._resolve_scope(job, config)
        snapshot = self._reconstruct_snapshot(job)
        source_root = AuthorizedRoot.create(watch.root)
        output_root = AuthorizedRoot.create(watch.library_root)
        scan = FilesystemScanResult(source_root, snapshot)
        candidates = SnapshotCandidateSource.from_scanned(snapshot)
        compiler = FilesystemPlanCompiler(scan, output_root)
        subtitle_provider = FilesystemSubtitleSampleProvider(scan)
        session_id = run_id
        event_store = PostgresEventStore(
            self.pool,
            run_id=run_id,
            plans=self.plans,
        )
        state = event_store.state
        if (
            state is not None
            and state.phase is Phase.BUILD_PLAN
            and state.rename_plan is not None
            and state.plan_hash is not None
        ):
            state = event_store.append(
                ApprovalRequested(plan_hash=state.plan_hash)
            )
        if (
            state is not None
            and state.phase is Phase.AWAITING_APPROVAL
            and state.plan_hash is not None
        ):
            if state.status is not RunStatus.STOPPED:
                event_store.append(
                    RunStopped(reason=StopReason.AWAITING_APPROVAL)
                )
            return state.plan_hash
        current_definition = organizer_definition(work_type)
        try:
            definition, bound_session_id = self.definitions.load_bound(
                run_id=run_id
            )
        except ServerError as error:
            if error.code is not ServerErrorCode.INTERACTION_CONFLICT:
                raise
            self.definitions.register_and_bind(
                run_id=run_id,
                definition=current_definition,
                session_id=session_id,
            )
            definition = current_definition
            bound_session_id = session_id
        if (
            not is_supported_organizer_definition(
                definition,
                work_type,
                allow_v1=False,
            )
            or bound_session_id != session_id
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        session = RepositoryAgentSession(
            repository=self.sessions,
            run_id=run_id,
            session_id=session_id,
        )
        secret = self.secrets.load(config.provider.secret_ref)
        model = self.model_factory(config, secret)
        tmdb = self.tmdb_factory()
        try:
            context = create_organizer_context(
                run_id=run_id,
                candidate_source=candidates,
                work_type=work_type,
                tmdb_provider=tmdb.provider,
                archive_browser=FilesystemArchiveDirectoryBrowser(
                    run_id=run_id,
                    root=output_root,
                ),
                subtitle_provider=subtitle_provider,
                plan_compiler=compiler,
                plan_store=self.plans,
                budget=config.agent_budget,
                event_store=event_store,
                agent_session=session,
            )
            try:
                result = await run_episode_organizer(
                    context=context,
                    model=model.model,
                    model_settings=model.model_settings,
                    prompt=_INITIAL_PROMPT,
                    instructions=definition.instructions,
                )
            except (MaxTurnsExceeded, BudgetExceeded) as error:
                if (
                    event_store.state is not None
                    and event_store.state.retryable_directory_failure
                ):
                    raise ArchiveDirectoryError(
                        "directory_io_unavailable",
                        retryable=True,
                    ) from error
                raise
            if (
                result.state.phase is not Phase.AWAITING_APPROVAL
                or result.state.plan_hash is None
            ):
                raise ValueError("initial agent did not produce an approvable plan")
            return result.state.plan_hash
        finally:
            await tmdb.close()
            await model.close()

    @staticmethod
    def _resolve_scope(
        job: AgentJobContext,
        config: ConfigRevision,
    ) -> tuple[WatchConfig, TmdbWorkType]:
        watch = next(
            (
                item
                for item in config.watches
                if item.watch_id == job.discovery.watch_id
            ),
            None,
        )
        if watch is None or watch.work_type is not job.registration.work_type:
            raise ValueError("run scope does not match exact config revision")
        work_type = {
            ServerWorkType.ANIME: TmdbWorkType.ANIME,
            ServerWorkType.TV: TmdbWorkType.TV_SERIES,
            ServerWorkType.MOVIE: TmdbWorkType.MOVIE,
        }[watch.work_type]
        return watch, work_type

    @staticmethod
    def _reconstruct_snapshot(job: AgentJobContext):
        persisted = job.discovery.snapshot
        if persisted is None:
            raise ValueError("discovery snapshot is unavailable")
        snapshot = build_candidate_snapshot(
            ScannedFile(
                relative_path=item.relative_path,
                kind=item.kind,
                size_bytes=item.size_bytes,
                device=item.device,
                inode=item.inode,
                mtime_ns=item.mtime_ns,
                ctime_ns=item.ctime_ns,
                sample_digest=item.sample_digest,
            )
            for item in persisted.files
        )
        if snapshot.snapshot_id != persisted.snapshot_id:
            raise ValueError("discovery snapshot identity mismatch")
        return snapshot
