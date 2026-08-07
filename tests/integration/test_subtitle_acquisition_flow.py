from __future__ import annotations

import asyncio
import errno
import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

import reeloom.executor.subtitle_acquisition as executor_module
from reeloom.adapters.approval import FilesystemApprovalStore
from reeloom.adapters.subtitle_journal import (
    FilesystemSubtitleAcquisitionJournalStore,
)
from reeloom.adapters.subtitle_plan_store import (
    FilesystemSubtitleAcquisitionPlanStore,
)
from reeloom.executor.subtitle_acquisition import SubtitleAcquisitionExecutor
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.rename_plan import RootBinding
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
from reeloom.server.subtitle_successor import (
    InMemorySubtitleSuccessorOutbox,
    SubtitleAcquisitionSettlement,
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


def _native_rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    try:
        os.stat(
            destination_name,
            dir_fd=destination_parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(errno.EEXIST, "destination exists")
    os.rename(
        source_name,
        destination_name,
        src_dir_fd=source_parent_fd,
        dst_dir_fd=destination_parent_fd,
    )


def test_selected_archive_becomes_one_fresh_run_without_acquisition_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "media"
    source = media / "Anime"
    workspace = tmp_path / "workspace"
    plan_root = tmp_path / "plans"
    approval_root = tmp_path / "approvals"
    journal_root = tmp_path / "journals"
    for path in (
        source,
        workspace,
        plan_root,
        approval_root,
        journal_root,
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
    planner = SubtitleAcquisitionPlanner(boundary, boundary, plans)
    plan = asyncio.run(
        planner.build(
            SubtitleAcquisitionPlanningRequest(
                run_id="run-m13-e2e",
                config_revision_id="config-1",
                created_at=_NOW,
                source_root=RootBinding(
                    PurePosixPath(media.as_posix()),
                    root.device,
                    root.inode,
                ),
                source_folder="Anime",
                source_folder_device=original.device,
                source_folder_inode=original.inode,
                folder_generation_id="generation-origin",
                candidate_snapshot_id=original.candidates.snapshot_id,
                tmdb_id=123,
                decision=SubtitleSelectionDecision.selected(
                    (SubtitleSelection(1, capability.archive_set_id),)
                ),
                capabilities=(capability,),
            )
        )
    )

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
    executor = SubtitleAcquisitionExecutor(
        plans,
        approvals,
        FilesystemSubtitleAcquisitionJournalStore(
            AuthorizedRoot.create(journal_root)
        ),
        boundary,
        boundary,
    )
    monkeypatch.setattr(
        executor_module,
        "_rename_noreplace",
        _native_rename_noreplace,
    )

    result = asyncio.run(
        executor.apply(
            plan_hash=plan.plan_hash,
            approval_id=approval.approval_id,
        )
    )
    settlement = SubtitleAcquisitionSettlement.create(
        plan=plan,
        result=result,
        origin_discovery_id="discovery-origin",
    )
    outbox = InMemorySubtitleSuccessorOutbox()
    outbox.register_origin(
        run_id=plan.run_id,
        discovery_id="discovery-origin",
        watch_id="watch-anime",
        config_revision=1,
        source_folder="Anime",
        snapshot_id=original.candidates.snapshot_id,
    )
    outbox.settle(settlement)
    claim = outbox.claim(
        worker_id="worker-e2e",
        now=_NOW,
        lease_for=timedelta(seconds=30),
    )
    assert claim is not None
    fresh = watcher.scan_folder(
        root,
        PurePosixPath("Anime"),
        logical_name="Anime",
    )
    assert not outbox.stabilize(
        claim,
        snapshot=fresh,
        now=_NOW,
        delay=timedelta(seconds=1),
    )
    claim = outbox.claim(
        worker_id="worker-e2e",
        now=_NOW + timedelta(seconds=1),
        lease_for=timedelta(seconds=30),
    )
    assert claim is not None
    assert outbox.stabilize(
        claim,
        snapshot=fresh,
        now=_NOW + timedelta(seconds=1),
        delay=timedelta(seconds=1),
    )
    successor = outbox.complete(
        claim,
        snapshot=fresh,
        now=_NOW + timedelta(seconds=1),
    )

    assert result.published_count == 1
    assert fresh.candidates.snapshot_id != original.candidates.snapshot_id
    assert any(
        item.kind is CandidateKind.SUBTITLE
        for item in successor.discovery.snapshot.files
    )
    assert not outbox.lineage_allows_automatic_acquisition(
        successor.registration.run_id
    )
