from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from reeloom.adapters.subtitle_plan_store import (
    FilesystemSubtitleAcquisitionPlanStore,
)
from reeloom.adapters.subtitle_archive_cache import (
    FilesystemSubtitleArchiveCache,
)
from reeloom.kernel.semantic_identity import SemanticRootBinding
from reeloom.kernel.subtitle_acquisition import (
    CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION,
    CURRENT_SUBTITLE_SEARCH_PARSER_VERSION,
    CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION,
    InspectedSubtitleMember,
    SubtitleArchiveFormat,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleArchiveSource,
    SubtitleArchiveVolume,
    SubtitleReleaseId,
    SubtitleSelection,
    SubtitleSelectionDecision,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.subtitle_acquisition import (
    DownloadedArchiveVolume,
    DownloadedSubtitleArchiveSet,
    InspectedSubtitleArchiveSet,
    SubtitleArchiveError,
    SubtitleArchiveErrorCode,
)
from reeloom.server.subtitle_acquisition import (
    SubtitleAcquisitionPlanner,
    SubtitleAcquisitionPlanningRequest,
)
from reeloom.server.watcher import NoFollowWatcher


def _capability() -> SubtitleArchiveSetCapability:
    return SubtitleArchiveSetCapability(
        SubtitleArchiveSetId(1),
        SubtitleReleaseId(1),
        SubtitleArchiveFormat.ZIP,
        10081,
        95257,
        (34768,),
        16,
    )


@dataclass
class _Fetcher:
    workspace_root: Path
    capability: SubtitleArchiveSetCapability
    calls: int = 0
    provider_version: str = CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION
    parser_version: str = CURRENT_SUBTITLE_SEARCH_PARSER_VERSION

    async def fetch(
        self,
        capability: SubtitleArchiveSetCapability,
    ) -> DownloadedSubtitleArchiveSet:
        self.calls += 1
        assert capability == self.capability
        path = self.workspace_root / "download.zip"
        if not path.exists():
            path.write_bytes(b"PK\x03\x04archive")
        metadata = path.stat()
        return DownloadedSubtitleArchiveSet(
            capability,
            (
                DownloadedArchiveVolume(
                    SubtitleArchiveVolume(
                        1,
                        34768,
                        metadata.st_size,
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    ),
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
    inspector_version: str = CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION

    async def inspect(
        self,
        downloaded: DownloadedSubtitleArchiveSet,
        *,
        season_numbers: tuple[int, ...],
    ) -> InspectedSubtitleArchiveSet:
        content = b"subtitle"
        return InspectedSubtitleArchiveSet(
            SubtitleArchiveSource(
                downloaded.capability.release_id,
                downloaded.capability.archive_set_id,
                downloaded.capability.format,
                season_numbers,
                downloaded.capability.thread_id,
                downloaded.capability.post_id,
                "c" * 64,
                tuple(item.volume for item in downloaded.volumes),
            ),
            (
                InspectedSubtitleMember(
                    downloaded.capability.archive_set_id,
                    PurePosixPath("Subs/E01.ass"),
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                ),
            ),
            (),
        )


def _request(source: Path, capability: SubtitleArchiveSetCapability):
    folder = source / "release"
    if not (folder / "episode.mkv").exists():
        (folder / "episode.mkv").write_bytes(b"video")
    current = NoFollowWatcher().scan_folder(
        AuthorizedRoot.create(source),
        PurePosixPath("release"),
        logical_name="release",
    )
    return SubtitleAcquisitionPlanningRequest(
        run_id="run-m13-planner",
        config_revision=1,
        config_revision_id="config-1",
        watch_id="watch-anime",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        source_root=SemanticRootBinding(PurePosixPath(source.as_posix())),
        source_folder="release",
        folder_generation_id="generation-1",
        inventory_id=current.semantic_inventory_id,
        candidate_snapshot=current.candidates.semantic_snapshot,
        tmdb_id=123,
        decision=SubtitleSelectionDecision.selected(
            (
                SubtitleSelection(1, capability.archive_set_id),
                SubtitleSelection(2, capability.archive_set_id),
            )
        ),
        capabilities=(capability,),
    )


def test_planner_fetches_shared_archive_once_and_persists_canonical_plan(
    tmp_path,
) -> None:
    source = tmp_path / "media"
    workspace = tmp_path / "workspace"
    plan_root = tmp_path / "plans"
    source.mkdir()
    (source / "release").mkdir()
    workspace.mkdir()
    plan_root.mkdir()
    capability = _capability()
    fetcher = _Fetcher(workspace, capability)
    store = FilesystemSubtitleAcquisitionPlanStore(
        AuthorizedRoot.create(plan_root)
    )
    planner = SubtitleAcquisitionPlanner(fetcher, _Inspector(), store)

    plan = asyncio.run(planner.build(_request(source, capability)))

    assert fetcher.calls == 1
    assert plan.archives[0].season_numbers == (1, 2)
    assert plan.members[0].destination_name.endswith(".ass")
    assert store.load(plan.plan_hash) == plan.canonical_bytes()


def test_planner_persists_verified_download_in_content_cache(tmp_path) -> None:
    source = tmp_path / "media"
    workspace = tmp_path / "workspace"
    plan_root = tmp_path / "plans"
    cache_root = tmp_path / "cache"
    source.mkdir()
    (source / "release").mkdir()
    workspace.mkdir()
    plan_root.mkdir()
    cache_root.mkdir()
    capability = _capability()
    fetcher = _Fetcher(workspace, capability)
    cache = FilesystemSubtitleArchiveCache(
        AuthorizedRoot.create(cache_root)
    )
    planner = SubtitleAcquisitionPlanner(
        fetcher,
        _Inspector(),
        FilesystemSubtitleAcquisitionPlanStore(
            AuthorizedRoot.create(plan_root)
        ),
        cache,
    )

    plan = asyncio.run(planner.build(_request(source, capability)))
    cached = cache.load(capability, plan.archives[0].volumes)

    assert cached is not None
    assert cached.volumes[0].path.parent == cache_root


def test_planner_rejects_workspace_inside_media_root_before_fetch(tmp_path) -> None:
    source = tmp_path / "media"
    workspace = source / "workspace"
    plan_root = tmp_path / "plans"
    source.mkdir()
    (source / "release").mkdir()
    workspace.mkdir()
    plan_root.mkdir()
    capability = _capability()
    fetcher = _Fetcher(workspace, capability)
    planner = SubtitleAcquisitionPlanner(
        fetcher,
        _Inspector(),
        FilesystemSubtitleAcquisitionPlanStore(
            AuthorizedRoot.create(plan_root)
        ),
    )

    with pytest.raises(SubtitleArchiveError) as raised:
        asyncio.run(planner.build(_request(source, capability)))
    assert raised.value.code is SubtitleArchiveErrorCode.CAPABILITY_CHANGED
    assert fetcher.calls == 0


def test_planner_rejects_provider_or_inspector_version_drift(tmp_path) -> None:
    source = tmp_path / "media"
    workspace = tmp_path / "workspace"
    plan_root = tmp_path / "plans"
    source.mkdir()
    (source / "release").mkdir()
    workspace.mkdir()
    plan_root.mkdir()
    capability = _capability()
    fetcher = _Fetcher(workspace, capability, provider_version="changed")
    planner = SubtitleAcquisitionPlanner(
        fetcher,
        _Inspector(),
        FilesystemSubtitleAcquisitionPlanStore(
            AuthorizedRoot.create(plan_root)
        ),
    )

    with pytest.raises(SubtitleArchiveError) as raised:
        asyncio.run(planner.build(_request(source, capability)))
    assert raised.value.code is SubtitleArchiveErrorCode.CAPABILITY_CHANGED
    assert fetcher.calls == 0


def test_planner_ignores_metadata_churn_when_semantic_identity_is_unchanged(
    tmp_path,
) -> None:
    source = tmp_path / "media"
    workspace = tmp_path / "workspace"
    plan_root = tmp_path / "plans"
    source.mkdir()
    (source / "release").mkdir()
    workspace.mkdir()
    plan_root.mkdir()
    capability = _capability()
    fetcher = _Fetcher(workspace, capability)
    request = _request(source, capability)
    (source / "release" / "episode.mkv").touch()
    planner = SubtitleAcquisitionPlanner(
        fetcher,
        _Inspector(),
        FilesystemSubtitleAcquisitionPlanStore(
            AuthorizedRoot.create(plan_root)
        ),
    )

    plan = asyncio.run(planner.build(request))

    assert plan.candidate_snapshot == request.candidate_snapshot
    assert b'"inode"' not in plan.canonical_bytes()
    assert fetcher.calls == 1
