from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.kernel.rename_plan import RootBinding
from reeloom.kernel.subtitle_acquisition import (
    CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION,
    CURRENT_SUBTITLE_SEARCH_PARSER_VERSION,
    CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION,
    SubtitleAcquisitionPlan,
    SubtitleArchiveSetCapability,
    SubtitleSelectionDecision,
    SubtitleSelectionStatus,
)
from reeloom.ports.subtitle_acquisition import (
    SubtitleAcquisitionPlanStore,
    SubtitleArchiveCache,
    SubtitleArchiveError,
    SubtitleArchiveErrorCode,
    SubtitleArchiveFetcher,
    SubtitleArchiveInspector,
)


def _capability_error() -> SubtitleArchiveError:
    return SubtitleArchiveError(
        SubtitleArchiveErrorCode.CAPABILITY_CHANGED,
        retryable=False,
    )


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionPlanningRequest:
    run_id: str
    config_revision_id: str
    created_at: datetime
    source_root: RootBinding
    source_folder: str
    source_folder_device: int
    source_folder_inode: int
    folder_generation_id: str
    candidate_snapshot_id: str
    tmdb_id: int
    decision: SubtitleSelectionDecision
    capabilities: tuple[SubtitleArchiveSetCapability, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.decision, SubtitleSelectionDecision)
            or self.decision.status is not SubtitleSelectionStatus.SELECTED
            or not isinstance(self.capabilities, tuple)
            or any(
                not isinstance(item, SubtitleArchiveSetCapability)
                for item in self.capabilities
            )
            or len({item.archive_set_id for item in self.capabilities})
            != len(self.capabilities)
        ):
            raise DomainError(ErrorCode.INVALID_SUBTITLE_SELECTION)
        available = {item.archive_set_id for item in self.capabilities}
        if any(
            item.archive_set_id not in available
            for item in self.decision.selections
        ):
            raise DomainError(ErrorCode.INVALID_SUBTITLE_SELECTION)


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionPlanner:
    fetcher: SubtitleArchiveFetcher
    inspector: SubtitleArchiveInspector
    plans: SubtitleAcquisitionPlanStore
    cache: SubtitleArchiveCache | None = None

    async def build(
        self,
        request: SubtitleAcquisitionPlanningRequest,
    ) -> SubtitleAcquisitionPlan:
        if not isinstance(request, SubtitleAcquisitionPlanningRequest):
            raise _capability_error()
        if (
            self.fetcher.provider_version
            != CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION
            or self.fetcher.parser_version
            != CURRENT_SUBTITLE_SEARCH_PARSER_VERSION
            or self.inspector.inspector_version
            != CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION
        ):
            raise _capability_error()
        self._require_source_identity(request)
        self._require_disjoint_workspace(
            Path(request.source_root.path.as_posix()),
            self.fetcher.workspace_root,
        )
        capability_by_id = {
            item.archive_set_id: item for item in request.capabilities
        }
        seasons_by_archive: dict[object, list[int]] = {}
        for selection in request.decision.selections:
            seasons_by_archive.setdefault(selection.archive_set_id, []).append(
                selection.season_number
            )

        inspected = []
        for archive_set_id in sorted(seasons_by_archive):
            capability = capability_by_id.get(archive_set_id)
            if capability is None:
                raise _capability_error()
            downloaded = await self.fetcher.fetch(capability)
            if downloaded.capability != capability:
                raise _capability_error()
            if self.cache is not None:
                self._require_disjoint_workspace(
                    Path(request.source_root.path.as_posix()),
                    self.cache.cache_root,
                )
                downloaded = self.cache.store(downloaded)
            result = await self.inspector.inspect(
                downloaded,
                season_numbers=tuple(sorted(seasons_by_archive[archive_set_id])),
            )
            if (
                result.source.archive_set_id != archive_set_id
                or result.source.thread_id != capability.thread_id
                or result.source.post_id != capability.post_id
                or tuple(
                    volume.attachment_id for volume in result.source.volumes
                )
                != capability.attachment_ids
            ):
                raise _capability_error()
            inspected.append(result)

        plan = SubtitleAcquisitionPlan.create(
            run_id=request.run_id,
            config_revision_id=request.config_revision_id,
            created_at=request.created_at,
            source_root=request.source_root,
            source_folder=request.source_folder,
            source_folder_device=request.source_folder_device,
            source_folder_inode=request.source_folder_inode,
            folder_generation_id=request.folder_generation_id,
            candidate_snapshot_id=request.candidate_snapshot_id,
            tmdb_id=request.tmdb_id,
            archives=tuple(item.source for item in inspected),
            inspected_members=tuple(
                member for item in inspected for member in item.members
            ),
            rejected_entries=tuple(
                rejected
                for item in inspected
                for rejected in item.rejected_entries
            ),
        )
        try:
            self.plans.save(plan)
        except ExecutorError as error:
            if error.code is not ExecutorErrorCode.PLAN_ALREADY_EXISTS:
                raise
            if self.plans.load(plan.plan_hash) != plan.canonical_bytes():
                raise _capability_error() from None
        return plan

    @staticmethod
    def _require_source_identity(
        request: SubtitleAcquisitionPlanningRequest,
    ) -> None:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise _capability_error()
        root_fd: int | None = None
        folder_fd: int | None = None
        try:
            root_fd = os.open(
                request.source_root.path.as_posix(),
                os.O_RDONLY
                | os.O_DIRECTORY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0),
            )
            root_metadata = os.fstat(root_fd)
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise _capability_error()
            folder_fd = os.open(
                request.source_folder,
                os.O_RDONLY
                | os.O_DIRECTORY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=root_fd,
            )
            folder_metadata = os.fstat(folder_fd)
            if not stat.S_ISDIR(folder_metadata.st_mode):
                raise _capability_error()
        except SubtitleArchiveError:
            raise
        except OSError:
            raise _capability_error() from None
        finally:
            if folder_fd is not None:
                os.close(folder_fd)
            if root_fd is not None:
                os.close(root_fd)

    @staticmethod
    def _require_disjoint_workspace(source_root: Path, workspace_root: Path) -> None:
        if (
            not source_root.is_absolute()
            or not workspace_root.is_absolute()
        ):
            raise _capability_error()
        try:
            common = Path(os.path.commonpath((source_root, workspace_root)))
        except ValueError:
            return
        if common in {source_root, workspace_root}:
            raise _capability_error()
