from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from psycopg_pool import ConnectionPool

from reeloom.adapters.filesystem import (
    FilesystemPlanCompiler,
    FilesystemScanResult,
    FilesystemSubtitleSampleProvider,
)
from reeloom.agents.organizer import create_organizer_context
from reeloom.agents.organizer import run_episode_organizer
from reeloom.kernel.scanner import ScannedFile, build_candidate_snapshot
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.plans import PlanStore
from reeloom.ports.tmdb import TmdbProvider
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.state import Phase
from reeloom.server.agent_repository import (
    PostgresAgentDefinitionRepository,
)
from reeloom.server.config import (
    ArchiveRoute,
    ConfigRevision,
    ServerWorkType,
    WatchConfig,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.inventory import ArchiveInventoryProvider
from reeloom.server.organizer_definition import organizer_definition
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
    "validated episode mapping for its configured work type."
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
    budget: RunBudget = RunBudget()

    async def run(self, *, run_id: str) -> str:
        job = self.scheduler.get_job_context(run_id=run_id)
        config = self.configs.get(job.registration.config_revision)
        watch, archive, work_type = self._resolve_scope(job, config)
        snapshot = self._reconstruct_snapshot(job)
        source_root = AuthorizedRoot.create(watch.root)
        output_root = AuthorizedRoot.create(archive.root)
        scan = FilesystemScanResult(source_root, snapshot)
        candidates = SnapshotCandidateSource.from_scanned(snapshot)
        compiler = FilesystemPlanCompiler(scan, output_root)
        subtitle_provider = FilesystemSubtitleSampleProvider(scan)
        session_id = run_id
        definition = organizer_definition(work_type)
        self.definitions.register_and_bind(
            run_id=run_id,
            definition=definition,
            session_id=session_id,
        )
        event_store = PostgresEventStore(
            self.pool,
            run_id=run_id,
            plans=self.plans,
        )
        if (
            event_store.state is not None
            and event_store.state.phase is Phase.AWAITING_APPROVAL
            and event_store.state.plan_hash is not None
        ):
            return event_store.state.plan_hash
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
                inventory=ArchiveInventoryProvider(output_root),
                subtitle_provider=subtitle_provider,
                plan_compiler=compiler,
                plan_store=self.plans,
                budget=self.budget,
                event_store=event_store,
                agent_session=session,
            )
            result = await run_episode_organizer(
                context=context,
                model=model.model,
                model_settings=model.model_settings,
                prompt=_INITIAL_PROMPT,
                instructions=definition.instructions,
            )
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
    ) -> tuple[WatchConfig, ArchiveRoute, TmdbWorkType]:
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
        archive = next(
            (
                item
                for item in config.archive_routes
                if item.work_type is watch.work_type
            ),
            None,
        )
        if archive is None:
            raise ValueError("archive route missing from exact config revision")
        if watch.work_type is ServerWorkType.MOVIE:
            raise ValueError("movie episode mapping is not supported")
        work_type = (
            TmdbWorkType.ANIME
            if watch.work_type is ServerWorkType.ANIME
            else TmdbWorkType.TV_SERIES
        )
        return watch, archive, work_type

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
