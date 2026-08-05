from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from agents import MaxTurnsExceeded
from psycopg_pool import ConnectionPool

from reeloom.adapters.filesystem import (
    FilesystemPlanCompiler,
    FilesystemScanResult,
    FilesystemSubtitleSampleProvider,
    FilesystemVideoSubtitleInspector,
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
from reeloom.ports.subtitle_acquisition import SubtitleSearchProvider
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
from reeloom.server.organizer_definition import organizer_definition
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.provider import ModelLease
from reeloom.server.runtime_store import PostgresEventStore
from reeloom.server.notification_projector import (
    PostgresNotificationProjector,
)
from reeloom.server.scheduler import AgentJobContext
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.session import (
    PostgresSessionRepository,
    RepositoryAgentSession,
)
from reeloom.server.secrets import FilesystemSecretStore
from reeloom.server.subtitle_acquisition import (
    SubtitleAcquisitionPlanner,
    SubtitleAcquisitionPlanningRequest,
)
from reeloom.kernel.subtitle_acquisition import SubtitleAcquisitionPlan
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


class SubtitleSearchLease(Protocol):
    @property
    def provider(self) -> SubtitleSearchProvider: ...

    async def close(self) -> None: ...


class SubtitleSearchLeaseFactory(Protocol):
    def __call__(self) -> SubtitleSearchLease: ...


class SubtitlePlanningLease(Protocol):
    @property
    def planner(self) -> SubtitleAcquisitionPlanner: ...

    async def close(self) -> None: ...


class SubtitlePlanningLeaseFactory(Protocol):
    def __call__(self) -> SubtitlePlanningLease: ...


class SubtitlePlanSink(Protocol):
    def __call__(self, plan: SubtitleAcquisitionPlan) -> object: ...


class SubtitleLineageGate(Protocol):
    def lineage_allows_automatic_acquisition(self, run_id: str) -> bool: ...


class AgentWorkKind(StrEnum):
    MEDIA_PLAN = "media_plan"
    SUBTITLE_ACQUISITION = "subtitle_acquisition"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True, slots=True)
class AgentWorkResult:
    kind: AgentWorkKind
    plan_hash: str | None = None


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
    notifications: PostgresNotificationProjector | None = None
    subtitle_search_factory: SubtitleSearchLeaseFactory | None = None
    subtitle_planning_factory: SubtitlePlanningLeaseFactory | None = None
    subtitle_plan_sink: SubtitlePlanSink | None = None
    subtitle_lineage_gate: SubtitleLineageGate | None = None

    async def run(self, *, run_id: str) -> str:
        result = await self.run_result(run_id=run_id)
        if result.plan_hash is None:
            raise ValueError("agent work did not produce a plan")
        return result.plan_hash

    async def run_result(self, *, run_id: str) -> AgentWorkResult:
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
            notifications=self.notifications,
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
            return AgentWorkResult(
                AgentWorkKind.MEDIA_PLAN, state.plan_hash
            )
        if (
            state is not None
            and state.phase is Phase.BUILD_SUBTITLE_ACQUISITION_PLAN
        ):
            plan = await self._build_subtitle_acquisition_plan(
                job=job,
                config=config,
                compiler=compiler,
                state=state,
            )
            return AgentWorkResult(
                AgentWorkKind.SUBTITLE_ACQUISITION,
                plan.plan_hash,
            )
        if state is not None and state.stop_reason is StopReason.NEEDS_ATTENTION:
            return AgentWorkResult(AgentWorkKind.NEEDS_ATTENTION)
        search_enabled = (
            work_type is TmdbWorkType.ANIME
            and config.acgrip.enabled
            and (
                self.subtitle_lineage_gate is None
                or self.subtitle_lineage_gate
                .lineage_allows_automatic_acquisition(run_id)
            )
        )
        current_definition = organizer_definition(
            work_type,
            subtitle_acquisition_enabled=search_enabled,
        )
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
            definition != current_definition
            or bound_session_id != session_id
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        session = RepositoryAgentSession(
            repository=self.sessions,
            run_id=run_id,
            session_id=session_id,
        )
        if search_enabled and self.subtitle_search_factory is None:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        secret = self.secrets.load(config.provider.secret_ref)
        model = self.model_factory(config, secret)
        tmdb = self.tmdb_factory()
        subtitle_search = None
        try:
            if search_enabled:
                assert self.subtitle_search_factory is not None
                subtitle_search = self.subtitle_search_factory()
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
                video_subtitle_inspector=(
                    FilesystemVideoSubtitleInspector(scan)
                    if search_enabled
                    else None
                ),
                subtitle_search_provider=(
                    None
                    if subtitle_search is None
                    else subtitle_search.provider
                ),
                subtitle_acquisition_enabled=search_enabled,
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
                    tool_names=definition.tools,
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
            if result.state.stop_reason is StopReason.NEEDS_ATTENTION:
                return AgentWorkResult(AgentWorkKind.NEEDS_ATTENTION)
            if (
                result.state.phase
                is Phase.BUILD_SUBTITLE_ACQUISITION_PLAN
            ):
                plan = await self._build_subtitle_acquisition_plan(
                    job=job,
                    config=config,
                    compiler=compiler,
                    state=result.state,
                )
                return AgentWorkResult(
                    AgentWorkKind.SUBTITLE_ACQUISITION,
                    plan.plan_hash,
                )
            if (
                result.state.phase is not Phase.AWAITING_APPROVAL
                or result.state.plan_hash is None
            ):
                raise ValueError("initial agent did not produce an approvable plan")
            return AgentWorkResult(
                AgentWorkKind.MEDIA_PLAN,
                result.state.plan_hash,
            )
        finally:
            if subtitle_search is not None:
                await subtitle_search.close()
            await tmdb.close()
            await model.close()

    async def _build_subtitle_acquisition_plan(
        self,
        *,
        job: AgentJobContext,
        config: ConfigRevision,
        compiler: FilesystemPlanCompiler,
        state: object,
    ) -> SubtitleAcquisitionPlan:
        decision = getattr(state, "subtitle_selection_decision", None)
        selected = getattr(state, "selected_series", None)
        capabilities = getattr(state, "subtitle_archive_capabilities", None)
        discovery = job.discovery
        if (
            self.subtitle_planning_factory is None
            or self.subtitle_plan_sink is None
            or decision is None
            or selected is None
            or not isinstance(capabilities, tuple)
            or discovery.source_folder is None
            or discovery.folder_generation_id is None
            or discovery.source_folder_device is None
            or discovery.source_folder_inode is None
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        lease = self.subtitle_planning_factory()
        try:
            plan = await lease.planner.build(
                SubtitleAcquisitionPlanningRequest(
                    run_id=job.registration.run_id,
                    config_revision_id=config.revision_id,
                    created_at=discovery.discovered_at,
                    source_root=compiler.source_root_binding,
                    source_folder=discovery.source_folder,
                    source_folder_device=discovery.source_folder_device,
                    source_folder_inode=discovery.source_folder_inode,
                    folder_generation_id=discovery.folder_generation_id,
                    candidate_snapshot_id=discovery.snapshot_id,
                    tmdb_id=selected.tmdb_id,
                    decision=decision,
                    capabilities=capabilities,
                )
            )
            self.subtitle_plan_sink(plan)
            return plan
        finally:
            await lease.close()

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
