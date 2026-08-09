from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol

from agents import MaxTurnsExceeded
from psycopg_pool import ConnectionPool

from reeloom.adapters.filesystem import (
    FilesystemPlanCompiler,
    FilesystemPlanCompilerV2,
    FilesystemScanResult,
    FilesystemSubtitleSampleProvider,
    FilesystemVideoSubtitleInspector,
)
from reeloom.agents.organizer import (
    create_organizer_context,
    run_episode_organizer,
)
from reeloom.kernel.scanner import (
    ScannedCandidateSnapshot,
    ScannedFile,
    build_candidate_snapshot,
)
from reeloom.kernel.semantic_identity import SemanticCandidateSnapshot
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.archive_directory import ArchiveDirectoryError
from reeloom.ports.plans import PlanCompiler, PlanStore
from reeloom.ports.tmdb import TmdbProvider
from reeloom.ports.subtitle_acquisition import (
    SubtitleSearchProvider,
    VideoSubtitleInspector,
)
from reeloom.runtime.errors import BudgetExceeded
from reeloom.runtime.events import (
    ApprovalRequested,
    RunStopped,
    SubtitleAcquisitionPlanCompleted,
)
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
    SubtitleAcquisitionPolicy,
    SubtitleProvider,
    WatchConfig,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.organizer_definition import organizer_definition
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.job_outcome import (
    AgentWorkFailure,
    FailureEnvelope,
    FailureStage,
)
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
from reeloom.server.watcher import NoFollowWatcher
from reeloom.kernel.subtitle_acquisition import SubtitleAcquisitionPlanV2
from reeloom.tools.candidates import SnapshotCandidateSource

_INITIAL_PROMPT = (
    "Inspect the authorized candidate snapshot and submit one complete, "
    "validated mapping for its configured work type."
)
_LOGGER = logging.getLogger(__name__)


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
    def __call__(self, plan: SubtitleAcquisitionPlanV2) -> object: ...


class SubtitleLineageGate(Protocol):
    def lineage_allows_automatic_acquisition(self, run_id: str) -> bool: ...


class VideoSubtitleInspectorFactory(Protocol):
    def __call__(
        self,
        scan: FilesystemScanResult,
        snapshot_id: str | None,
    ) -> VideoSubtitleInspector: ...


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
    video_subtitle_inspector_factory: (
        VideoSubtitleInspectorFactory | None
    ) = None

    async def run(self, *, run_id: str) -> str:
        result = await self.run_result(run_id=run_id)
        if result.plan_hash is None:
            raise ValueError("agent work did not produce a plan")
        return result.plan_hash

    async def run_result(self, *, run_id: str) -> AgentWorkResult:
        job = self.scheduler.get_job_context(run_id=run_id)
        config = self.configs.get(job.registration.config_revision)
        watch, work_type = self._resolve_scope(job, config)
        source_root = AuthorizedRoot.create(watch.root)
        output_root = AuthorizedRoot.create(watch.library_root)
        snapshot, semantic_snapshot = self._resolve_snapshots(
            job, source_root
        )
        scan = FilesystemScanResult(source_root, snapshot)
        if semantic_snapshot is None:
            candidates = SnapshotCandidateSource.from_scanned(snapshot)
            compiler: PlanCompiler = FilesystemPlanCompiler(
                scan, output_root
            )
            snapshot_id_override = None
        else:
            candidates = SnapshotCandidateSource.from_semantic(
                semantic_snapshot
            )
            compiler = FilesystemPlanCompilerV2(
                scan=scan,
                semantic_snapshot=semantic_snapshot,
                output_root=output_root,
                config_revision=config.revision,
                watch_id=watch.watch_id,
            )
            snapshot_id_override = semantic_snapshot.snapshot_id
        subtitle_provider = FilesystemSubtitleSampleProvider(
            scan, snapshot_id_override=snapshot_id_override
        )
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
            if (
                watch.subtitle_acquisition.policy
                is SubtitleAcquisitionPolicy.PLAN_ONLY
            ):
                event_store.append(
                    SubtitleAcquisitionPlanCompleted(plan.plan_hash)
                )
            return AgentWorkResult(
                AgentWorkKind.SUBTITLE_ACQUISITION,
                plan.plan_hash,
            )
        if state is not None and state.stop_reason is StopReason.NEEDS_ATTENTION:
            return AgentWorkResult(AgentWorkKind.NEEDS_ATTENTION)
        lineage_allows_acquisition = (
            self.subtitle_lineage_gate is None
            or self.subtitle_lineage_gate
            .lineage_allows_automatic_acquisition(run_id)
        )
        search_enabled = self._subtitle_search_enabled(
            watch,
            work_type,
            lineage_allows_acquisition=lineage_allows_acquisition,
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
            raise AgentWorkFailure(
                FailureEnvelope(
                    code="subtitle_plan_context_unavailable",
                    stage=FailureStage.SUBTITLE_PLAN,
                )
            )
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
                    (
                        self.video_subtitle_inspector_factory(
                            scan, snapshot_id_override
                        )
                        if self.video_subtitle_inspector_factory is not None
                        else FilesystemVideoSubtitleInspector(
                            scan,
                            snapshot_id_override=snapshot_id_override,
                        )
                    )
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
                if (
                    watch.subtitle_acquisition.policy
                    is SubtitleAcquisitionPolicy.PLAN_ONLY
                ):
                    event_store.append(
                        SubtitleAcquisitionPlanCompleted(plan.plan_hash)
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
            leases = (
                *((subtitle_search,) if subtitle_search is not None else ()),
                tmdb,
                model,
            )
            for lease in leases:
                try:
                    await lease.close()
                except Exception:
                    _LOGGER.exception("failed to close agent adapter lease")

    async def _build_subtitle_acquisition_plan(
        self,
        *,
        job: AgentJobContext,
        config: ConfigRevision,
        compiler: PlanCompiler,
        state: object,
    ) -> SubtitleAcquisitionPlanV2:
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
            or not isinstance(compiler, FilesystemPlanCompilerV2)
            or discovery.source_folder is None
            or discovery.folder_generation_id is None
            or discovery.inventory_id is None
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        lease = self.subtitle_planning_factory()
        try:
            try:
                plan = await lease.planner.build(
                    SubtitleAcquisitionPlanningRequest(
                        run_id=job.registration.run_id,
                        config_revision=config.revision,
                        config_revision_id=config.revision_id,
                        watch_id=job.discovery.watch_id,
                        created_at=discovery.discovered_at,
                        source_root=compiler.source_root_binding,
                        source_folder=discovery.source_folder,
                        folder_generation_id=discovery.folder_generation_id,
                        inventory_id=discovery.inventory_id,
                        candidate_snapshot=compiler.semantic_snapshot,
                        tmdb_id=selected.tmdb_id,
                        decision=decision,
                        capabilities=capabilities,
                    )
                )
                self.subtitle_plan_sink(plan)
            except ServerError as error:
                if error.code is ServerErrorCode.DATABASE_UNAVAILABLE:
                    raise
                raise AgentWorkFailure(
                    FailureEnvelope(
                        code=error.code.value,
                        stage=FailureStage.SUBTITLE_PLAN,
                    )
                ) from error
            except AgentWorkFailure:
                raise
            except Exception as error:
                raise AgentWorkFailure(
                    FailureEnvelope(
                        code="subtitle_plan_failed",
                        stage=FailureStage.SUBTITLE_PLAN,
                    )
                ) from error
            return plan
        finally:
            try:
                await lease.close()
            except Exception:
                # The immutable plan may already be durable. Transport cleanup
                # cannot invalidate it or strand the run before registration.
                _LOGGER.exception("failed to close subtitle planning lease")

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
    def _subtitle_search_enabled(
        watch: WatchConfig,
        work_type: TmdbWorkType,
        *,
        lineage_allows_acquisition: bool,
    ) -> bool:
        return (
            work_type is TmdbWorkType.ANIME
            and watch.work_type is ServerWorkType.ANIME
            and watch.subtitle_acquisition.enabled
            and watch.subtitle_acquisition.provider
            is SubtitleProvider.ACGRIP
            and lineage_allows_acquisition
        )

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

    @classmethod
    def _resolve_snapshots(
        cls,
        job: AgentJobContext,
        source_root: AuthorizedRoot,
    ) -> tuple[
        ScannedCandidateSnapshot,
        SemanticCandidateSnapshot | None,
    ]:
        if not job.discovery.snapshot_id.startswith(
            "candidate-snapshot-v2:"
        ):
            return cls._reconstruct_snapshot(job), None
        discovery = job.discovery
        persisted = discovery.snapshot
        if persisted is None or discovery.source_folder is None:
            raise ValueError("semantic discovery snapshot is unavailable")
        expected = persisted.semantic_snapshot
        if expected.snapshot_id != discovery.snapshot_id:
            raise ValueError("semantic discovery identity mismatch")
        current = NoFollowWatcher().scan_folder(
            source_root,
            PurePosixPath(discovery.source_folder),
            logical_name=discovery.source_folder,
        )
        semantic = current.candidates.semantic_snapshot
        if semantic.snapshot_id != discovery.snapshot_id:
            raise ValueError("semantic discovery is no longer current")
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
            for item in current.candidates.files
        )
        return snapshot, semantic
