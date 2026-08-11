from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest
from agents import ModelSettings

from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.adapters.subtitle_plan_store import (
    FilesystemSubtitleAcquisitionPlanStore,
)
from reeloom.adapters.subtitle_archive_cache import (
    FilesystemSubtitleArchiveCache,
)
from reeloom.executor.subtitle_marker_acquisition import (
    SubtitleMarkerAcquisitionExecutor,
)
from reeloom.executor.errors import ApprovalError, ApprovalErrorCode
from reeloom.kernel.subtitle_publication import SUBTITLE_PUBLICATION_MARKER
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.forward_execution import ExecutionOperation
from reeloom.agents.organizer import EPISODE_ORGANIZER_INSTRUCTIONS
from reeloom.agents.scripted_model import ScriptedModel, ToolCallStep
from reeloom.kernel.specials import SpecialKind
from reeloom.kernel.subtitle_acquisition import (
    CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION,
    CURRENT_SUBTITLE_SEARCH_PARSER_VERSION,
    CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION,
    EmbeddedChineseStatus,
    EmbeddedSubtitleInspection,
    EmbeddedSubtitleProbeStatus,
    InspectedSubtitleMember,
    SubtitleAcquisitionPlanV2,
    SubtitleArchiveFormat,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleArchiveSetSummary,
    SubtitleArchiveSource,
    SubtitleArchiveVolume,
    SubtitleReleaseId,
    SubtitleReleaseSummary,
    SubtitleSearchPage,
    SubtitleSearchDiagnostics,
)
from reeloom.kernel.tmdb import (
    TmdbEpisode,
    TmdbLanguage,
    TmdbSearchCandidate,
    TmdbSeasonDetails,
    TmdbSeriesDetails,
    TmdbWorkType,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.budget import RunBudget
from reeloom.server.agent_definition import AgentDefinitionRevision
from reeloom.server.agent_repository import (
    PostgresAgentDefinitionRepository,
)
from reeloom.server.agent_worker import InitialAgentWorker
from reeloom.server.approval_repository import PostgresApprovalStore
from reeloom.server.api_models import RunResponse
from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraftInput,
    ProviderConfigInput,
    ServerWorkType,
    SubtitleAcquisitionConfig,
    SubtitleAcquisitionPolicy,
    SubtitleProvider,
    WatchConfig,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.config_service import ConfigService
from reeloom.server.database import PostgresControlPlane
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.runtime_store import PostgresEventStore
from reeloom.server.organizer_definition import (
    LEGACY_EPISODE_ORGANIZER_TOOL_NAMES,
    LEGACY_ORGANIZER_SCHEMA_VERSION,
    ORGANIZER_NAME,
)
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.secrets import FilesystemSecretStore
from reeloom.server.session import PostgresSessionRepository
from reeloom.server.subtitle_acquisition import SubtitleAcquisitionPlanner
from reeloom.server.subtitle_acquisition_service import (
    SubtitleAcquisitionCoordinator,
)
from reeloom.server.forward_operation_repository import (
    ForwardOperationError,
    ForwardOperationErrorCode,
    PostgresForwardOperationRepository,
    execution_operation_id,
)
from reeloom.server.forward_rescan import ForwardRescanWorker
from reeloom.server.queries import PostgresQueries
from reeloom.server.subtitle_lineage import PostgresSubtitleLineageGate
from reeloom.server.watcher import NoFollowWatcher
from reeloom.ports.subtitle_acquisition import (
    DownloadedArchiveVolume,
    DownloadedSubtitleArchiveSet,
    InspectedSubtitleArchiveSet,
    SubtitleSearchRequest,
    SubtitleSearchResult,
)


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


class _Tmdb:
    async def search_titles(
        self,
        *,
        query: str,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
        limit: int,
        include_adult: bool = True,
    ) -> tuple[TmdbSearchCandidate, ...]:
        del query, language, limit, include_adult
        return (
            TmdbSearchCandidate(
                tmdb_id=200,
                localized_name="测试动画",
                original_name="Test Anime",
                year=2024,
                original_language="ja",
                work_type=work_type,
            ),
        )

    async def get_series(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
    ) -> TmdbSeriesDetails:
        return TmdbSeriesDetails(
            tmdb_id=tmdb_id,
            language=language,
            localized_name="测试动画",
            original_name="Test Anime",
            first_air_year=2024,
            seasons=(),
            work_type=work_type,
        )

    async def get_season(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        season_number: int,
        language: TmdbLanguage,
    ) -> TmdbSeasonDetails:
        return TmdbSeasonDetails(
            tmdb_id=tmdb_id,
            language=language,
            season_number=season_number,
            episodes=(
                TmdbEpisode(
                    season_number=season_number,
                    episode_number=1,
                    name="第一集",
                    overview="",
                    special_kind=SpecialKind.UNKNOWN,
                ),
            ),
            work_type=work_type,
        )


class _TmdbLease:
    provider = _Tmdb()
    closed = False

    async def close(self) -> None:
        self.closed = True


class _ModelLease:
    def __init__(self, model: ScriptedModel) -> None:
        self.model = model
        self.model_settings = ModelSettings()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class _AbsentVideoInspector:
    snapshot_id: str
    candidate_count: int

    async def inspect(self, video_id, *, season_number: int):
        return EmbeddedSubtitleInspection(
            video_id,
            season_number,
            EmbeddedSubtitleProbeStatus.ABSENT,
            EmbeddedChineseStatus.ABSENT,
            (),
        )


class _SubtitleSearch:
    provider_version = (
        f"{CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION}+"
        f"{CURRENT_SUBTITLE_SEARCH_PARSER_VERSION}"
    )

    async def search(
        self, request: SubtitleSearchRequest
    ) -> SubtitleSearchResult:
        archive_id = SubtitleArchiveSetId(1)
        release_id = SubtitleReleaseId(1)
        return SubtitleSearchResult(
            SubtitleSearchPage(
                (
                    SubtitleReleaseSummary(
                        release_id,
                        (
                            SubtitleArchiveSetSummary(
                                archive_id,
                                SubtitleArchiveFormat.ZIP,
                                1,
                                16,
                            ),
                        ),
                        "测试动画 中文字幕",
                        "原生附件",
                        "S00",
                        ("简体中文",),
                        (),
                        ("标题匹配",),
                        (),
                        True,
                    ),
                ),
                None,
                True,
            ),
            (
                SubtitleArchiveSetCapability(
                    archive_id,
                    release_id,
                    SubtitleArchiveFormat.ZIP,
                    10806,
                    95257,
                    (34768,),
                    16,
                ),
            ),
            SubtitleSearchDiagnostics(
                request.title_aliases,
                tuple(1 for _ in request.title_aliases),
                1,
                1,
                1,
                1,
                1,
                1,
                1,
            ),
        )


class _SubtitleSearchLease:
    provider = _SubtitleSearch()

    async def close(self) -> None:
        return None


@dataclass
class _ArchiveFetcher:
    workspace_root: Path
    provider_version: str = CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION
    parser_version: str = CURRENT_SUBTITLE_SEARCH_PARSER_VERSION

    async def fetch(self, capability):
        content = b"PK\x03\x04archive"
        path = self.workspace_root / "subtitle.zip"
        path.write_bytes(content)
        metadata = path.stat()
        volume = SubtitleArchiveVolume(
            1,
            capability.attachment_ids[0],
            len(content),
            hashlib.sha256(content).hexdigest(),
        )
        return DownloadedSubtitleArchiveSet(
            capability,
            (
                DownloadedArchiveVolume(
                    volume,
                    path,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                ),
            ),
        )


class _ArchiveInspector:
    inspector_version = CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION

    async def inspect(self, downloaded, *, season_numbers):
        subtitle = b"[Script Info]\n"
        capability = downloaded.capability
        return InspectedSubtitleArchiveSet(
            SubtitleArchiveSource(
                capability.release_id,
                capability.archive_set_id,
                capability.format,
                season_numbers,
                capability.thread_id,
                capability.post_id,
                "c" * 64,
                tuple(item.volume for item in downloaded.volumes),
            ),
            (
                InspectedSubtitleMember(
                    capability.archive_set_id,
                    PurePosixPath("Subs/E01.ass"),
                    len(subtitle),
                    hashlib.sha256(subtitle).hexdigest(),
                ),
            ),
            (),
        )

    async def extract_member(self, downloaded, member):
        del downloaded, member
        return b"[Script Info]\n"


@dataclass
class _PlanningLease:
    planner: SubtitleAcquisitionPlanner

    async def close(self) -> None:
        return None


@dataclass
class _ExecutorLease:
    executor: SubtitleMarkerAcquisitionExecutor

    async def close(self) -> None:
        return None


class _PoisonApprovalStore:
    def issue_or_reuse(self, approval: object) -> object:
        del approval
        raise ApprovalError(ApprovalErrorCode.INVALID_RECORD)


def _model() -> ScriptedModel:
    return ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="list",
            ),
            ToolCallStep(
                name="search_tmdb",
                arguments={"query": "Test Anime", "work_type": "anime"},
                call_id="search",
            ),
            ToolCallStep(
                name="select_series",
                arguments={"tmdb_id": 200, "work_type": "anime"},
                call_id="select",
            ),
            ToolCallStep(
                name="get_tmdb_season",
                arguments={
                    "tmdb_id": 200,
                    "work_type": "anime",
                    "season_number": 1,
                    "language": "zh-CN",
                },
                call_id="season",
            ),
            ToolCallStep(
                name="search_dir",
                arguments={
                    "mode": "selected_tmdb_id",
                    "name": None,
                    "cursor": 0,
                    "limit": 50,
                },
                call_id="archive-search",
            ),
            ToolCallStep(
                name="submit_mapping",
                arguments={
                    "videos": [
                        {
                            "video_id": "video:1",
                            "season": 1,
                            "episode_start": 1,
                            "episode_end": 1,
                        }
                    ],
                    "subtitles": [],
                    "review": {
                        "summary": "唯一视频已映射为 S01E01。",
                        "unmapped_explanations": [],
                    },
                },
                call_id="mapping",
            ),
        )
    )


def _subtitle_model() -> ScriptedModel:
    return ScriptedModel(
        (
            ToolCallStep(
                "search_tmdb",
                {"query": "Test Anime", "work_type": "anime"},
                "subtitle-search-tmdb",
            ),
            ToolCallStep(
                "get_tmdb_series",
                {
                    "tmdb_id": 200,
                    "work_type": "anime",
                    "language": "zh-CN",
                },
                "subtitle-series",
            ),
            ToolCallStep(
                "select_series",
                {"tmdb_id": 200, "work_type": "anime"},
                "subtitle-select-series",
            ),
            ToolCallStep(
                "get_tmdb_season",
                {
                    "tmdb_id": 200,
                    "work_type": "anime",
                    "season_number": 0,
                    "language": "zh-CN",
                },
                "subtitle-season",
            ),
            ToolCallStep(
                "check_sub_from_video",
                {"video_id": "video:1", "season_number": 0},
                "subtitle-probe",
            ),
            ToolCallStep(
                "search_sub",
                {"season_number": 0, "cursor": None},
                "subtitle-forum-search",
            ),
            ToolCallStep(
                "select_subtitle_release",
                {
                    "selections": [
                        {
                            "season_number": 0,
                            "archive_set_id": "subarchive:1",
                        }
                    ],
                    "needs_attention_reason": None,
                },
                "subtitle-release-select",
            ),
        )
    )


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("subtitle_policy", "scenario"),
    (
        (
            SubtitleAcquisitionPolicy.PLAN_ONLY,
            "plan_register_crash",
        ),
        (SubtitleAcquisitionPolicy.MANUAL, "marker_crash"),
        (SubtitleAcquisitionPolicy.AUTOMATIC, "success"),
        (SubtitleAcquisitionPolicy.AUTOMATIC, "existing_successor"),
        (SubtitleAcquisitionPolicy.AUTOMATIC, "generation_conflict"),
        (SubtitleAcquisitionPolicy.AUTOMATIC, "publication_collision"),
        (SubtitleAcquisitionPolicy.AUTOMATIC, "lease_exhausted"),
        (SubtitleAcquisitionPolicy.AUTOMATIC, "approval_poison"),
    ),
)
def test_semantic_subtitle_selection_builds_and_persists_v2_plan(
    tmp_path: Path,
    subtitle_policy: SubtitleAcquisitionPolicy,
    scenario: str,
) -> None:
    control = PostgresControlPlane(_dsn())
    incoming = tmp_path / "incoming-subtitle"
    release = incoming / "Test Anime OAD"
    archive = tmp_path / "archive-subtitle"
    secret_root = tmp_path / "secrets-subtitle"
    media_plan_root = tmp_path / "media-plans-subtitle"
    subtitle_plan_root = tmp_path / "subtitle-plans"
    workspace = tmp_path / "subtitle-workspace"
    subtitle_cache_root = tmp_path / "subtitle-cache"
    for root in (
        release,
        archive,
        secret_root,
        media_plan_root,
        subtitle_plan_root,
        workspace,
        subtitle_cache_root,
    ):
        root.mkdir(parents=True, exist_ok=True)
    (release / "episode.mkv").write_bytes(b"video")
    try:
        control.open()
        control.migrate()
        configs = PostgresConfigRepository(control.pool)
        previous = configs.head()
        expected = 0 if previous is None else previous.revision
        secrets = FilesystemSecretStore(AuthorizedRoot.create(secret_root))
        watch_id = f"watch-{uuid.uuid4().hex}"
        revision = ConfigService(
            configs=configs,
            secrets=secrets,
        ).compare_and_append(
            expected_revision=expected,
            value=ConfigDraftInput(
                watches=(
                    WatchConfig(
                        watch_id=watch_id,
                        root=incoming,
                        library_root=archive,
                        work_type=ServerWorkType.ANIME,
                        poll_interval_seconds=1,
                        settle_interval_seconds=1,
                        subtitle_acquisition=SubtitleAcquisitionConfig(
                            enabled=True,
                            provider=SubtitleProvider.ACGRIP,
                            policy=subtitle_policy,
                        ),
                    ),
                ),
                provider=ProviderConfigInput(
                    base_url="https://api.openai.com/v1",
                    model="gpt-test",
                    api_key=b"not-a-real-secret",
                ),
                apply_policy=ApplyPolicy.MANUAL,
            ),
        )
        scheduler = PostgresSchedulerRepository(control.pool)
        scheduler.configure_watch(
            watch_id=watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            work_type=ServerWorkType.ANIME,
            settle_interval_seconds=1,
            semantic_v2=True,
        )
        watcher = NoFollowWatcher()
        scan = watcher.scan_folders(AuthorizedRoot.create(incoming))
        observed = datetime.now(UTC)
        scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            observed_at=observed,
            scan=scan,
        )
        discovery = scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            observed_at=observed + timedelta(seconds=1),
            scan=scan,
        ).discoveries[0]
        assert discovery.source_folder_device is None
        assert discovery.source_folder_inode is None
        with control.pool.connection() as connection:
            atomic_registration = connection.execute(
                """
                SELECT run.run_id, job.status
                FROM runs AS run
                JOIN jobs AS job USING (run_id)
                WHERE run.discovery_id = %s
                """,
                (discovery.discovery_id,),
            ).fetchone()
        assert atomic_registration is not None
        assert atomic_registration[1] == "pending"
        registration = scheduler.register_run(
            discovery_id=discovery.discovery_id
        )
        assert registration.run_id == atomic_registration[0]
        subtitle_plans = FilesystemSubtitleAcquisitionPlanStore(
            AuthorizedRoot.create(subtitle_plan_root)
        )
        fetcher = _ArchiveFetcher(workspace)
        inspector = _ArchiveInspector()
        cache = FilesystemSubtitleArchiveCache(
            AuthorizedRoot.create(subtitle_cache_root)
        )
        operations = PostgresForwardOperationRepository(control.pool)
        marker_executor = SubtitleMarkerAcquisitionExecutor(
            subtitle_plans,
            cache,
            fetcher,
            inspector,
        )
        coordinator = SubtitleAcquisitionCoordinator(
            pool=control.pool,
            plans=subtitle_plans,
            executor_factory=lambda: _ExecutorLease(marker_executor),
            operation_approvals=PostgresApprovalStore(control.pool),
            operations=operations,
            worker_id="subtitle-test-worker",
        )

        def assert_no_legacy_subtitle_effects() -> None:
            with control.pool.connection() as connection:
                legacy = connection.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM run_operations
                         WHERE run_id = %s),
                        (SELECT count(*) FROM subtitle_scan_requests_v2
                         WHERE run_id = %s),
                        (SELECT count(*) FROM folder_housekeeping_v2
                         WHERE run_id = %s),
                        (SELECT count(*)
                         FROM subtitle_acquisition_settlements
                         WHERE origin_run_id = %s),
                        (SELECT count(*)
                         FROM subtitle_publication_settlements_v2
                         WHERE origin_run_id = %s)
                    """,
                    (registration.run_id,) * 5,
                ).fetchone()
            assert legacy is not None
            assert tuple(int(item) for item in legacy) == (0, 0, 0, 0, 0)

        sink_attempts = 0

        def plan_sink(plan: SubtitleAcquisitionPlanV2) -> object:
            nonlocal sink_attempts
            sink_attempts += 1
            if scenario == "plan_register_crash" and sink_attempts == 1:
                raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE)
            return coordinator.register_plan(plan)

        model_attempts = 0

        def model_factory(config, secret):
            nonlocal model_attempts
            del config, secret
            model_attempts += 1
            return _ModelLease(
                _subtitle_model()
                if model_attempts == 1
                else ScriptedModel(())
            )

        worker = InitialAgentWorker(
            scheduler=scheduler,
            configs=configs,
            definitions=PostgresAgentDefinitionRepository(control.pool),
            sessions=PostgresSessionRepository(control.pool),
            secrets=secrets,
            plans=FilesystemPlanStore(
                AuthorizedRoot.create(media_plan_root)
            ),
            model_factory=model_factory,
            tmdb_factory=lambda: _TmdbLease(),
            pool=control.pool,
            subtitle_search_factory=lambda: _SubtitleSearchLease(),
            subtitle_planning_factory=lambda: _PlanningLease(
                SubtitleAcquisitionPlanner(
                    fetcher,
                    inspector,
                    subtitle_plans,
                    cache,
                )
            ),
            subtitle_plan_sink=plan_sink,
            video_subtitle_inspector_factory=(
                lambda scan_result, snapshot_id: _AbsentVideoInspector(
                    snapshot_id or scan_result.snapshot.snapshot_id,
                    len(scan_result.snapshot.records),
                )
            ),
        )
        boot_id = f"subtitle-test-boot-{uuid.uuid4().hex}"
        control.register_boot(boot_id)
        with control.pool.connection() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'running', boot_id = %s,
                                updated_at = clock_timestamp()
                WHERE job_id = %s AND run_id = %s AND status = 'pending'
                """,
                (boot_id, registration.job_id, registration.run_id),
            )

        if scenario == "plan_register_crash":
            with pytest.raises(ServerError) as interrupted:
                asyncio.run(worker.run_result(run_id=registration.run_id))
            assert (
                interrupted.value.code
                is ServerErrorCode.DATABASE_UNAVAILABLE
            )
        result = asyncio.run(worker.run_result(run_id=registration.run_id))
        assert model_attempts == 1
        assert result.plan_hash is not None
        assert result.kind.value == "subtitle_acquisition"
        plan = SubtitleAcquisitionPlanV2.from_canonical_bytes(
            subtitle_plans.load(result.plan_hash),
            plan_hash=result.plan_hash,
        )
        assert plan.schema_version == "subtitle-acquisition-plan-v2"
        assert plan.watch_id == watch_id
        assert plan.inventory_id == discovery.inventory_id
        assert plan.candidate_snapshot_id == discovery.snapshot_id
        assert subtitle_plans.load(plan.plan_hash) == plan.canonical_bytes()
        canonical = plan.canonical_bytes()
        assert b'"device"' not in canonical
        assert b'"inode"' not in canonical
        assert b'"mtime"' not in canonical
        assert b'"ctime"' not in canonical
        request = None
        if scenario == "approval_poison":
            poisoned = SubtitleAcquisitionCoordinator(
                pool=control.pool,
                plans=subtitle_plans,
                executor_factory=lambda: _ExecutorLease(marker_executor),
                operation_approvals=_PoisonApprovalStore(),  # type: ignore[arg-type]
                operations=operations,
                worker_id="subtitle-poison-worker",
            )

            assert poisoned.reconcile_approved() == 0
            assert poisoned.reconcile_approved() == 0
            request = poisoned.resolve(
                run_id=registration.run_id,
                plan_hash=result.plan_hash,
            )
            assert request is not None
            assert request.status == "blocked"
            assert request.failure_code == (
                "automatic_subtitle_start_approval_unavailable"
            )
            with pytest.raises(ForwardOperationError) as missing:
                operations.get(
                    execution_operation_id(
                        run_id=registration.run_id,
                        plan_hash=result.plan_hash,
                    )
                )
            assert missing.value.code is (
                ForwardOperationErrorCode.OPERATION_NOT_FOUND
            )
            response = RunResponse.model_validate(
                PostgresQueries(control.pool).get_run(registration.run_id)
            )
            assert response.status == "failed"
            assert "delete_run" in response.available_actions
            assert_no_legacy_subtitle_effects()
            return
        if scenario == "generation_conflict":
            with control.pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO generation_requests_v2
                        (request_id, request_kind, origin_run_id, watch_id,
                         source_folder, expected_inventory_id,
                         generation_nonce)
                    VALUES (%s, 'legacy_handoff', %s, %s, %s, %s, %s)
                    """,
                    (
                        f"generation-active-{uuid.uuid4().hex}",
                        registration.run_id,
                        watch_id,
                        plan.source_folder,
                        plan.inventory_id,
                        f"generation-active-nonce-{uuid.uuid4().hex}",
                    ),
                )
        if scenario == "publication_collision":
            collision = release / plan.destination_directory.as_posix()
            collision.mkdir()
            (collision / "unexpected.txt").write_text(
                "occupied", encoding="utf-8"
            )
        if scenario in {
            "success",
            "existing_successor",
            "generation_conflict",
            "publication_collision",
        }:
            request = coordinator.approve_and_execute(
                run_id=registration.run_id,
                plan_hash=result.plan_hash,
                automatic=True,
            )
        # The v2 plan handoff atomically settles the planning job.  A caller
        # must not perform a second, independently inferred settlement.
        before = RunResponse.model_validate(
            PostgresQueries(control.pool).get_run(registration.run_id)
        )
        if subtitle_policy is SubtitleAcquisitionPolicy.MANUAL:
            assert "execute" in before.available_actions
            assert "approve_subtitle_acquisition" not in (
                before.available_actions
            )
        else:
            assert "approve_subtitle_acquisition" not in (
                before.available_actions
            )
        if subtitle_policy is SubtitleAcquisitionPolicy.PLAN_ONLY:
            request = coordinator.resolve(
                run_id=registration.run_id,
                plan_hash=result.plan_hash,
            )
            assert request is not None
            assert request.status == "planned"
            assert not (
                release / plan.destination_directory.as_posix()
            ).exists()
            with pytest.raises(ForwardOperationError) as missing:
                operations.get(
                    execution_operation_id(
                        run_id=registration.run_id,
                        plan_hash=plan.plan_hash,
                    )
                )
            assert (
                missing.value.code
                is ForwardOperationErrorCode.OPERATION_NOT_FOUND
            )
            assert before.phase == "completed"
            assert before.runtime_status == "stopped"
            assert "delete_run" in before.available_actions
            assert_no_legacy_subtitle_effects()
            return
        if scenario in {"marker_crash", "lease_exhausted"}:
            crash_time = datetime.now(UTC)
            approval = PostgresApprovalStore(
                control.pool
            ).issue_or_reuse(
                ApprovalRecord.create(
                    run_id=registration.run_id,
                    plan_hash=result.plan_hash,
                    scope=ApprovalScope.SUBTITLE_ACQUIRE,
                    expires_at=crash_time + timedelta(minutes=15),
                    nonce=uuid.uuid4().hex,
                )
            )
            coordinator._mark_approved(
                run_id=registration.run_id,
                plan_hash=result.plan_hash,
                approval_id=approval.approval_id,
            )
            operation_id = execution_operation_id(
                run_id=registration.run_id,
                plan_hash=result.plan_hash,
            )
            operations.authorize(
                ExecutionOperation.authorized(
                    operation_id=operation_id,
                    run_id=registration.run_id,
                    plan_hash=result.plan_hash,
                ),
                approval_id=approval.approval_id,
                now=crash_time,
                scope=ApprovalScope.SUBTITLE_ACQUIRE,
                operation_kind="subtitle_acquire",
            )
            crashed_lease = operations.claim(
                operation_id,
                worker_id="subtitle-crashed-worker",
                now=crash_time,
                lease_for=timedelta(seconds=1),
                operation_kind="subtitle_acquire",
            )
            assert crashed_lease is not None
            if scenario == "marker_crash":
                first_publication = asyncio.run(
                    marker_executor.execute_current(
                        plan_hash=result.plan_hash
                    )
                )
                assert first_publication.state.value == "completed"
            else:
                with control.pool.connection() as connection:
                    connection.execute(
                        """
                        UPDATE execution_operations_v2
                        SET attempt_count = 100,
                            lease_expires_at = %s
                        WHERE operation_id = %s
                          AND lease_owner = 'subtitle-crashed-worker'
                        """,
                        (crash_time, operation_id),
                    )
            restarted = SubtitleAcquisitionCoordinator(
                pool=control.pool,
                plans=subtitle_plans,
                executor_factory=lambda: _ExecutorLease(marker_executor),
                operation_approvals=PostgresApprovalStore(control.pool),
                operations=operations,
                worker_id="subtitle-restarted-worker",
                clock=lambda: crash_time + timedelta(seconds=2),
            )
            request = restarted.approve_and_execute(
                run_id=registration.run_id,
                plan_hash=result.plan_hash,
                automatic=(
                    subtitle_policy
                    is SubtitleAcquisitionPolicy.AUTOMATIC
                ),
            )
            restarted.reconcile_approved()
            request = restarted.resolve(
                run_id=registration.run_id,
                plan_hash=result.plan_hash,
            )
            with control.pool.connection() as connection:
                approval_count = connection.execute(
                    """
                    SELECT count(*) FROM approvals
                    WHERE run_id = %s AND plan_hash = %s
                      AND scope = 'subtitle_acquire'
                    """,
                    (registration.run_id, result.plan_hash),
                ).fetchone()
            assert approval_count is not None
            assert int(approval_count[0]) == 1
        if scenario == "lease_exhausted":
            assert request is not None
            assert request.status == "blocked"
            assert request.failure_code == "root_unavailable"
            assert not (
                release / plan.destination_directory.as_posix()
            ).exists()
            exhausted = operations.get(operation_id)
            assert exhausted.status.value == "unavailable"
            exhausted_view = operations.get_view(operation_id)
            assert exhausted_view.rescan_state == "queued"
            exhausted_response = RunResponse.model_validate(
                PostgresQueries(control.pool).get_run(registration.run_id)
            )
            assert exhausted_response.status == "failed"
            assert exhausted_response.lifecycle.rescan_state == "queued"
            assert "rescan" not in exhausted_response.available_actions
            assert "delete_run" in exhausted_response.available_actions
            assert "delete_run" in exhausted_response.available_actions
            assert_no_legacy_subtitle_effects()
            return
        if scenario in {
            "success",
            "existing_successor",
            "generation_conflict",
            "publication_collision",
        }:
            coordinator.reconcile_approved()
            request = coordinator.resolve(
                run_id=registration.run_id,
                plan_hash=result.plan_hash,
            )
        assert request is not None
        if scenario == "publication_collision":
            assert request.status == "blocked"
            assert request.failure_code == "destination_collision"
            assert request.failure_diagnostic == {
                "schema_version": 2,
                "stage": "publication",
                "reason": "unexpected_entry",
            }
            response = RunResponse.model_validate(
                PostgresQueries(control.pool).get_run(registration.run_id)
            )
            assert response.subtitle_acquisition is not None
            assert response.subtitle_acquisition.failure_diagnostic is not None
            assert (
                response.subtitle_acquisition.failure_diagnostic.reason
                == "unexpected_entry"
            )
            assert "delete_run" in response.available_actions
            assert_no_legacy_subtitle_effects()
            return
        assert request.status == "published"
        publication = release / plan.destination_directory.as_posix()
        assert (publication / plan.members[0].destination_name).is_file()
        assert (publication / SUBTITLE_PUBLICATION_MARKER).is_file()
        operation = operations.get(
            execution_operation_id(
                run_id=registration.run_id,
                plan_hash=plan.plan_hash,
            )
        )
        assert operation.status.value == "completed"
        view = operations.get_view(operation.operation_id)
        assert view.rescan_state == (
            "blocked" if scenario == "generation_conflict" else "queued"
        )
        if scenario == "generation_conflict":
            response = RunResponse.model_validate(
                PostgresQueries(control.pool).get_run(registration.run_id)
            )
            assert response.status == "completed"
            assert "rescan" in response.available_actions
            with control.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE generation_requests_v2
                    SET state = 'blocked', warning = 'test_owner_released'
                    WHERE watch_id = %s AND source_folder = %s
                      AND operation_id IS NULL AND state = 'queued'
                    """,
                    (watch_id, plan.source_folder),
                )
            operations.requeue_rescan(
                run_id=registration.run_id,
                plan_hash=plan.plan_hash,
                now=datetime.now(UTC),
            )
            assert operations.get_view(operation.operation_id).rescan_state == (
                "queued"
            )
            assert_no_legacy_subtitle_effects()
            return
        with control.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT r.status, j.status, s.phase, s.runtime_status,
                       o.operation_kind,
                       legacy.run_id IS NOT NULL
                FROM runs AS r
                JOIN jobs AS j ON j.run_id = r.run_id
                JOIN run_states AS s ON s.run_id = r.run_id
                JOIN execution_operations_v2 AS o ON o.run_id = r.run_id
                LEFT JOIN legacy_effect_supersessions_v2 AS legacy
                  ON legacy.run_id = r.run_id
                WHERE r.run_id = %s
                """,
                (registration.run_id,),
            ).fetchone()
        assert row is not None
        assert tuple(row[:5]) == (
            "superseded",
            "completed",
            "completed",
            "stopped",
            "subtitle_acquire",
        )
        assert not bool(row[5])
        response = RunResponse.model_validate(
            PostgresQueries(control.pool).get_run(registration.run_id)
        )
        assert response.recovery_approval_id is None
        assert response.execution is not None
        assert response.execution.operation_kind == "subtitle_acquire"
        assert "recover" not in response.available_actions
        assert "retry_subtitle_acquisition" not in response.available_actions
        assert "fail_subtitle_acquisition" not in response.available_actions
        assert "delete_run" in response.available_actions
        assert_no_legacy_subtitle_effects()

        rescan_worker = ForwardRescanWorker(
            operations=operations,
            scheduler=scheduler,
        )
        if scenario == "existing_successor":
            successor_scan = watcher.scan_folders(
                AuthorizedRoot.create(incoming)
            )
            successor_observed = datetime.now(UTC)
            scheduler.reconcile_folders(
                watch_id=watch_id,
                config_revision=revision.revision,
                fence=revision.revision,
                observed_at=successor_observed,
                scan=successor_scan,
            )
            successor_discovery = scheduler.reconcile_folders(
                watch_id=watch_id,
                config_revision=revision.revision,
                fence=revision.revision,
                observed_at=successor_observed + timedelta(seconds=1),
                scan=successor_scan,
            ).discoveries[0]
            successor = scheduler.register_run(
                discovery_id=successor_discovery.discovery_id
            )

            assert rescan_worker.process_one(
                worker_id=f"subtitle-rescan-{uuid.uuid4().hex}"
            )
            adopted = operations.get_view(operation.operation_id)
            assert adopted.rescan_state == "completed"
            assert adopted.successor_run_id == successor.run_id
            assert not PostgresSubtitleLineageGate(
                control.pool
            ).lineage_allows_automatic_acquisition(successor.run_id)
            assert_no_legacy_subtitle_effects()
            return
        for _ in range(20):
            if operations.get_view(operation.operation_id).rescan_state == (
                "accepted"
            ):
                break
            assert rescan_worker.process_one(
                worker_id=f"subtitle-rescan-{uuid.uuid4().hex}"
            )
        else:
            pytest.fail("subtitle generation request was not accepted")

        successor_scan = watcher.scan_folders(AuthorizedRoot.create(incoming))
        successor_observed = datetime.now(UTC)
        first_successor_poll = scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            observed_at=successor_observed,
            scan=successor_scan,
        )
        second_successor_poll = scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            observed_at=successor_observed + timedelta(seconds=1),
            scan=successor_scan,
        )
        successor_discoveries = (
            first_successor_poll.discoveries
            + second_successor_poll.discoveries
        )
        assert len(successor_discoveries) == 1
        successor = scheduler.register_run(
            discovery_id=successor_discoveries[0].discovery_id
        )
        repeated = scheduler.register_run(
            discovery_id=successor_discoveries[0].discovery_id
        )
        assert repeated.run_id == successor.run_id
        successor_view = operations.get_view(operation.operation_id)
        assert successor_view.successor_run_id == successor.run_id
        assert not PostgresSubtitleLineageGate(
            control.pool
        ).lineage_allows_automatic_acquisition(successor.run_id)
        with control.pool.connection() as connection:
            lineage = connection.execute(
                """
                SELECT subtitle_acquisition_lineage_key
                FROM runs WHERE run_id = %s
                """,
                (registration.run_id,),
            ).fetchone()
            successor_count = connection.execute(
                """
                SELECT count(*) FROM runs
                WHERE subtitle_acquisition_lineage_key = %s
                  AND run_id <> %s
                """,
                (str(lineage[0]), registration.run_id),
            ).fetchone()
        assert lineage is not None and lineage[0] is not None
        assert successor_count is not None
        assert int(successor_count[0]) == 1
    finally:
        control.close()


@pytest.mark.postgres
def test_initial_worker_runs_real_sdk_loop_and_resumes_identity(
    tmp_path: Path,
) -> None:
    control = PostgresControlPlane(_dsn())
    incoming = tmp_path / "incoming"
    archive = tmp_path / "archive"
    secret_root = tmp_path / "secrets"
    plan_root = tmp_path / "plans"
    for root in (incoming, archive, secret_root, plan_root):
        root.mkdir()
    (incoming / "untrusted S01E01.mkv").write_bytes(b"video")
    try:
        control.open()
        control.migrate()
        configs = PostgresConfigRepository(control.pool)
        previous = configs.head()
        expected = 0 if previous is None else previous.revision
        secrets = FilesystemSecretStore(
            AuthorizedRoot.create(secret_root)
        )
        revision = ConfigService(
            configs=configs,
            secrets=secrets,
        ).compare_and_append(
            expected_revision=expected,
            value=ConfigDraftInput(
                watches=(
                    WatchConfig(
                        watch_id=f"watch-{uuid.uuid4().hex}",
                        root=incoming,
                        library_root=archive,
                        work_type=ServerWorkType.ANIME,
                        poll_interval_seconds=1,
                        settle_interval_seconds=1,
                    ),
                ),
                provider=ProviderConfigInput(
                    base_url="https://api.openai.com/v1",
                    model="gpt-test",
                    api_key=b"not-a-real-secret",
                ),
                apply_policy=ApplyPolicy.MANUAL,
                agent_budget=RunBudget(
                    max_elapsed_seconds=321,
                ),
            ),
        )
        scheduler = PostgresSchedulerRepository(control.pool)
        watch = revision.watches[0]
        scheduler.configure_watch(
            watch_id=watch.watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            work_type=watch.work_type,
            settle_interval_seconds=1,
        )
        snapshot = NoFollowWatcher().scan(
            AuthorizedRoot.create(incoming)
        )
        observed = datetime.now(UTC)
        scheduler.reconcile_poll(
            watch_id=watch.watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            observed_at=observed,
            snapshot=snapshot,
        )
        settled = scheduler.reconcile_poll(
            watch_id=watch.watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            observed_at=observed + timedelta(seconds=1),
            snapshot=snapshot,
        )
        assert settled.discovery is not None
        registration = scheduler.register_run(
            discovery_id=settled.discovery.discovery_id
        )
        model = _model()
        model_lease = _ModelLease(model)
        tmdb_lease = _TmdbLease()
        plans = FilesystemPlanStore(
            AuthorizedRoot.create(plan_root)
        )
        worker = InitialAgentWorker(
            scheduler=scheduler,
            configs=configs,
            definitions=PostgresAgentDefinitionRepository(control.pool),
            sessions=PostgresSessionRepository(control.pool),
            secrets=secrets,
            plans=plans,
            model_factory=lambda config, secret: (
                model_lease
                if (
                    config.revision == revision.revision
                    and secret == b"not-a-real-secret"
                )
                else (_ for _ in ()).throw(AssertionError())
            ),
            tmdb_factory=lambda: tmdb_lease,
            pool=control.pool,
        )

        plan_hash = asyncio.run(worker.run(run_id=registration.run_id))
        recovered_hash = asyncio.run(worker.run(run_id=registration.run_id))

        assert recovered_hash == plan_hash
        assert model.exhausted
        assert model_lease.closed
        assert tmdb_lease.closed
        assert plans.load(plan_hash)
        state = PostgresEventStore(
            control.pool,
            run_id=registration.run_id,
            plans=plans,
        ).state
        assert state is not None
        assert state.plan_hash == plan_hash
        assert state.budget.max_elapsed_seconds == 321
        with control.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT r.agent_definition_hash, r.session_id, s.revision,
                       d.payload->>'instructions',
                       d.payload->>'schema_version', review.payload
                FROM runs AS r
                JOIN agent_sessions AS s ON s.run_id = r.run_id
                JOIN agent_definitions AS d
                  ON d.definition_hash = r.agent_definition_hash
                JOIN plan_reviews AS review
                  ON review.run_id = r.run_id
                 AND review.plan_hash = %s
                WHERE r.run_id = %s
                """,
                (plan_hash, registration.run_id),
            ).fetchone()
        assert row is not None
        assert str(row[1]) == registration.run_id
        assert int(row[2]) > 0
        assert EPISODE_ORGANIZER_INSTRUCTIONS in str(row[3])
        assert "check_sub_from_video" not in str(row[3])
        assert row[4] == "episode-organizer-v4"
        assert row[5]["status"] == "agent_and_system"
        assert row[5]["agent_summary"] == "唯一视频已映射为 S01E01。"

        legacy = AgentDefinitionRevision.create(
            name=ORGANIZER_NAME,
            instructions="Historical v1 organizer.",
            tools=LEGACY_EPISODE_ORGANIZER_TOOL_NAMES,
            schema_version=LEGACY_ORGANIZER_SCHEMA_VERSION,
        )
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO agent_definitions
                        (definition_hash, payload)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (definition_hash) DO NOTHING
                    """,
                    (legacy.definition_hash, legacy.to_json()),
                )
                connection.execute(
                    """
                    UPDATE runs
                    SET agent_definition_hash = %s
                    WHERE run_id = %s
                    """,
                    (legacy.definition_hash, registration.run_id),
                )

        assert (
            asyncio.run(worker.run(run_id=registration.run_id))
            == plan_hash
        )
    finally:
        control.close()
