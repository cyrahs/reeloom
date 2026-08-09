from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from reeloom.adapters.subtitle_archive_cache import (
    FilesystemSubtitleArchiveCache,
)
from reeloom.adapters.subtitle_plan_store import (
    FilesystemSubtitleAcquisitionPlanStore,
)
from reeloom.executor.subtitle_marker_acquisition import (
    SubtitleMarkerAcquisitionExecutor,
)
from reeloom.kernel.candidates import CandidateKind
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
)
from reeloom.server.subtitle_acquisition import (
    SubtitleAcquisitionPlanner,
    SubtitleAcquisitionPlanningRequest,
)
from reeloom.server.watcher import NoFollowWatcher

_NOW = datetime(2026, 8, 4, tzinfo=UTC)
_ARCHIVE = b"PK\x03\x04offline-archive"
_SUBTITLE = b"[Script Info]\nTitle: offline\n"


@dataclass
class _ArchiveBoundary:
    workspace_root: Path
    archive_path: Path
    capability: SubtitleArchiveSetCapability

    provider_version: str = CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION
    parser_version: str = CURRENT_SUBTITLE_SEARCH_PARSER_VERSION
    inspector_version: str = CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION

    async def fetch(
        self,
        capability: SubtitleArchiveSetCapability,
    ) -> DownloadedSubtitleArchiveSet:
        assert capability == self.capability
        metadata = os.stat(self.archive_path, follow_symlinks=False)
        volume = SubtitleArchiveVolume(
            1,
            capability.attachment_ids[0],
            len(_ARCHIVE),
            hashlib.sha256(_ARCHIVE).hexdigest(),
        )
        return DownloadedSubtitleArchiveSet(
            capability,
            (
                DownloadedArchiveVolume(
                    volume,
                    self.archive_path,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                ),
            ),
        )

    async def inspect(
        self,
        downloaded: DownloadedSubtitleArchiveSet,
        *,
        season_numbers: tuple[int, ...],
    ) -> InspectedSubtitleArchiveSet:
        member = InspectedSubtitleMember(
            downloaded.capability.archive_set_id,
            PurePosixPath("Subs/E01.ass"),
            len(_SUBTITLE),
            hashlib.sha256(_SUBTITLE).hexdigest(),
        )
        source = SubtitleArchiveSource(
            downloaded.capability.release_id,
            downloaded.capability.archive_set_id,
            downloaded.capability.format,
            season_numbers,
            downloaded.capability.thread_id,
            downloaded.capability.post_id,
            "d" * 64,
            tuple(item.volume for item in downloaded.volumes),
        )
        return InspectedSubtitleArchiveSet(source, (member,), ())

    async def extract_member(self, downloaded, member) -> bytes:
        del downloaded, member
        return _SUBTITLE


def test_selected_archive_v2_marker_becomes_visible_to_fresh_scan(
    tmp_path: Path,
) -> None:
    media = tmp_path / "media"
    source = media / "Anime"
    workspace = tmp_path / "workspace"
    plan_root = tmp_path / "plans"
    cache_root = tmp_path / "cache"
    for path in (
        source,
        workspace,
        plan_root,
        cache_root,
    ):
        path.mkdir(parents=True)
    (source / "episode.mkv").write_bytes(b"video")
    archive_path = workspace / "attachment-34768.zip"
    archive_path.write_bytes(_ARCHIVE)

    watcher = NoFollowWatcher()
    root = AuthorizedRoot.create(media)
    original = watcher.scan_folder(
        root,
        PurePosixPath("Anime"),
        logical_name="Anime",
    )
    capability = SubtitleArchiveSetCapability(
        SubtitleArchiveSetId(1),
        SubtitleReleaseId(1),
        SubtitleArchiveFormat.ZIP,
        10081,
        95257,
        (34768,),
        len(_ARCHIVE),
    )
    boundary = _ArchiveBoundary(workspace, archive_path, capability)
    plans = FilesystemSubtitleAcquisitionPlanStore(
        AuthorizedRoot.create(plan_root)
    )
    cache = FilesystemSubtitleArchiveCache(
        AuthorizedRoot.create(cache_root)
    )
    planner = SubtitleAcquisitionPlanner(boundary, boundary, plans, cache)
    plan = asyncio.run(
        planner.build(
            SubtitleAcquisitionPlanningRequest(
                run_id="run-m13-e2e",
                config_revision=1,
                config_revision_id="config-1",
                watch_id="watch-anime",
                created_at=_NOW,
                source_root=SemanticRootBinding(
                    PurePosixPath(media.as_posix())
                ),
                source_folder="Anime",
                folder_generation_id="generation-origin",
                inventory_id=original.semantic_inventory_id,
                candidate_snapshot=original.candidates.semantic_snapshot,
                tmdb_id=123,
                decision=SubtitleSelectionDecision.selected(
                    (SubtitleSelection(1, capability.archive_set_id),)
                ),
                capabilities=(capability,),
            )
        )
    )

    executor = SubtitleMarkerAcquisitionExecutor(
        plans,
        cache,
        boundary,
        boundary,
    )

    result = asyncio.run(
        executor.execute_current(plan_hash=plan.plan_hash)
    )
    fresh = watcher.scan_folder(
        root,
        PurePosixPath("Anime"),
        logical_name="Anime",
    )

    assert result.published_count == 1
    assert fresh.candidates.semantic_snapshot_id != (
        original.candidates.semantic_snapshot_id
    )
    assert any(
        item.kind is CandidateKind.SUBTITLE
        for item in fresh.candidates.files
    )
