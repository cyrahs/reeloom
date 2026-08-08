from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from reeloom.adapters.approval import FilesystemApprovalStore
from reeloom.adapters.subtitle_archive_cache import (
    FilesystemSubtitleArchiveCache,
)
from reeloom.adapters.subtitle_plan_store import (
    FilesystemSubtitleAcquisitionPlanStore,
)
from reeloom.executor.subtitle_marker_acquisition import (
    SubtitleMarkerAcquisitionExecutor,
)
from reeloom.executor.subtitle_publication import SubtitlePublicationState
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.rename_plan import RootBinding
from reeloom.kernel.subtitle_acquisition import (
    CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION,
    CURRENT_SUBTITLE_SEARCH_PARSER_VERSION,
    CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION,
    InspectedSubtitleMember,
    SubtitleAcquisitionPlan,
    SubtitleArchiveFormat,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleArchiveSource,
    SubtitleArchiveVolume,
    SubtitleReleaseId,
)
from reeloom.kernel.subtitle_publication import SUBTITLE_PUBLICATION_MARKER
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.subtitle_acquisition import (
    DownloadedArchiveVolume,
    DownloadedSubtitleArchiveSet,
    InspectedSubtitleArchiveSet,
)
from reeloom.server.watcher import NoFollowWatcher

_NOW = datetime(2026, 8, 7, tzinfo=UTC)
_ARCHIVE = b"PK\x03\x04fixed archive"
_SUBTITLE = b"[Script Info]\nTitle: cached\n"


@dataclass
class _Fetcher:
    workspace_root: Path
    source: SubtitleArchiveSource
    calls: int = 0
    provider_version: str = CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION
    parser_version: str = CURRENT_SUBTITLE_SEARCH_PARSER_VERSION

    async def fetch(
        self,
        capability: SubtitleArchiveSetCapability,
    ) -> DownloadedSubtitleArchiveSet:
        self.calls += 1
        path = self.workspace_root / "download.zip"
        path.write_bytes(_ARCHIVE)
        metadata = path.stat()
        return DownloadedSubtitleArchiveSet(
            capability,
            (
                DownloadedArchiveVolume(
                    self.source.volumes[0],
                    path,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                ),
            ),
        )


@dataclass
class _Inspector:
    source: SubtitleArchiveSource
    member: InspectedSubtitleMember
    inspect_calls: int = 0
    extract_calls: int = 0
    inspector_version: str = CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION

    async def inspect(
        self,
        downloaded: DownloadedSubtitleArchiveSet,
        *,
        season_numbers: tuple[int, ...],
    ) -> InspectedSubtitleArchiveSet:
        self.inspect_calls += 1
        assert season_numbers == self.source.season_numbers
        return InspectedSubtitleArchiveSet(
            self.source,
            (self.member,),
            (),
        )

    async def extract_member(self, downloaded, member) -> bytes:
        self.extract_calls += 1
        return _SUBTITLE


@dataclass(frozen=True)
class _Environment:
    media: Path
    source_folder: Path
    plan: SubtitleAcquisitionPlan
    approval: ApprovalRecord
    cache: FilesystemSubtitleArchiveCache
    fetcher: _Fetcher
    inspector: _Inspector
    executor: SubtitleMarkerAcquisitionExecutor


def _environment(tmp_path: Path, *, seed_cache: bool) -> _Environment:
    media = tmp_path / "media"
    source_folder = media / "release"
    workspace = tmp_path / "workspace"
    cache_root = tmp_path / "cache"
    plan_root = tmp_path / "plans"
    approval_root = tmp_path / "approvals"
    for path in (
        source_folder,
        workspace,
        cache_root,
        plan_root,
        approval_root,
    ):
        path.mkdir(parents=True, exist_ok=True)
    (source_folder / "episode.mkv").write_bytes(b"video")
    archive_id = SubtitleArchiveSetId(1)
    volume = SubtitleArchiveVolume(
        1,
        34768,
        len(_ARCHIVE),
        hashlib.sha256(_ARCHIVE).hexdigest(),
    )
    source = SubtitleArchiveSource(
        SubtitleReleaseId(1),
        archive_id,
        SubtitleArchiveFormat.ZIP,
        (1,),
        10081,
        95257,
        "b" * 64,
        (volume,),
    )
    member = InspectedSubtitleMember(
        archive_id,
        PurePosixPath("Subs/E01.ass"),
        len(_SUBTITLE),
        hashlib.sha256(_SUBTITLE).hexdigest(),
    )
    plan = SubtitleAcquisitionPlan.create(
        run_id="run-m14-marker-acquisition",
        config_revision_id="config-1",
        created_at=_NOW,
        source_root=RootBinding(
            PurePosixPath(media.as_posix()),
            999,
            999,
        ),
        source_folder=source_folder.name,
        source_folder_device=999,
        source_folder_inode=999,
        folder_generation_id="generation-1",
        candidate_snapshot_id="candidate-snapshot-v1:" + "a" * 64,
        tmdb_id=123,
        archives=(source,),
        inspected_members=(member,),
    )
    plans = FilesystemSubtitleAcquisitionPlanStore(
        AuthorizedRoot.create(plan_root)
    )
    plans.save(plan)
    approvals = FilesystemApprovalStore(
        AuthorizedRoot.create(approval_root),
        clock=lambda: _NOW,
    )
    approval = ApprovalRecord.create(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        scope=ApprovalScope.SUBTITLE_ACQUIRE,
        expires_at=_NOW + timedelta(minutes=15),
        nonce="n" * 32,
    )
    approvals.issue(approval)
    cache = FilesystemSubtitleArchiveCache(
        AuthorizedRoot.create(cache_root)
    )
    fetcher = _Fetcher(workspace, source)
    inspector = _Inspector(source, member)
    if seed_cache:
        capability = SubtitleArchiveSetCapability(
            archive_id,
            source.release_id,
            source.format,
            source.thread_id,
            source.post_id,
            (volume.attachment_id,),
            volume.size_bytes,
        )
        downloaded = asyncio.run(fetcher.fetch(capability))
        cache.store(downloaded)
        fetcher.calls = 0
    executor = SubtitleMarkerAcquisitionExecutor(
        plans,
        approvals,
        cache,
        fetcher,
        inspector,
    )
    return _Environment(
        media,
        source_folder,
        plan,
        approval,
        cache,
        fetcher,
        inspector,
        executor,
    )


def test_marker_executor_reuses_cache_and_ignores_persisted_stat_identity(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, seed_cache=True)

    result = asyncio.run(
        environment.executor.apply(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )
    )

    assert result.state is SubtitlePublicationState.COMPLETED
    assert environment.fetcher.calls == 0
    assert environment.inspector.inspect_calls == 1
    assert environment.inspector.extract_calls == 1
    destination = (
        environment.source_folder / environment.plan.destination_directory
    )
    assert (destination / SUBTITLE_PUBLICATION_MARKER).is_file()
    snapshot = NoFollowWatcher().scan_folder(
        AuthorizedRoot.create(environment.media),
        PurePosixPath(environment.source_folder.name),
        logical_name=environment.source_folder.name,
    )
    assert len(snapshot.candidates.files) == 2


def test_marker_executor_refetches_only_when_cache_is_missing(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, seed_cache=False)

    result = asyncio.run(
        environment.executor.apply(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )
    )

    assert result.state is SubtitlePublicationState.COMPLETED
    assert environment.fetcher.calls == 1
    assert environment.cache.load(
        SubtitleArchiveSetCapability(
            environment.plan.archives[0].archive_set_id,
            environment.plan.archives[0].release_id,
            environment.plan.archives[0].format,
            environment.plan.archives[0].thread_id,
            environment.plan.archives[0].post_id,
            tuple(
                item.attachment_id
                for item in environment.plan.archives[0].volumes
            ),
            sum(
                item.size_bytes
                for item in environment.plan.archives[0].volumes
            ),
        ),
        environment.plan.archives[0].volumes,
    ) is not None


def test_marker_executor_internal_reconcile_is_idempotent(tmp_path: Path) -> None:
    environment = _environment(tmp_path, seed_cache=True)
    asyncio.run(
        environment.executor.apply(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )
    )
    environment.inspector.extract_calls = 0

    result = asyncio.run(
        environment.executor.reconcile(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )
    )

    assert result.state is SubtitlePublicationState.COMPLETED
    assert environment.fetcher.calls == 0
    assert environment.inspector.extract_calls == 0


def test_marker_executor_preserves_destination_collision(tmp_path: Path) -> None:
    environment = _environment(tmp_path, seed_cache=True)
    destination = (
        environment.source_folder / environment.plan.destination_directory
    )
    destination.mkdir()
    collision = destination / environment.plan.members[0].destination_name
    collision.write_bytes(b"foreign")

    result = asyncio.run(
        environment.executor.apply(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )
    )

    assert result.state is SubtitlePublicationState.COLLISION
    assert collision.read_bytes() == b"foreign"
