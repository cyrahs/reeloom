from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.kernel.semantic_identity import (
    SemanticCandidateSnapshot,
    SemanticRootBinding,
)
from reeloom.kernel.subtitle_acquisition import (
    CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION,
    CURRENT_SUBTITLE_SEARCH_PARSER_VERSION,
    CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION,
    SubtitleAcquisitionPlanV2,
    SubtitleArchiveSetCapability,
    SubtitleSelectionDecision,
    SubtitleSelectionStatus,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.subtitle_acquisition import (
    SubtitleAcquisitionPlanStore,
    SubtitleArchiveCache,
    SubtitleArchiveError,
    SubtitleArchiveErrorCode,
    SubtitleArchiveFetcher,
    SubtitleArchiveInspector,
)
from reeloom.server.watcher import NoFollowWatcher


def _capability_error() -> SubtitleArchiveError:
    return SubtitleArchiveError(
        SubtitleArchiveErrorCode.CAPABILITY_CHANGED,
        retryable=False,
    )


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionPlanningRequest:
    run_id: str
    config_revision: int
    config_revision_id: str
    watch_id: str
    created_at: datetime
    source_root: SemanticRootBinding
    source_folder: str
    folder_generation_id: str
    inventory_id: str
    candidate_snapshot: SemanticCandidateSnapshot
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
            or type(self.config_revision) is not int
            or self.config_revision < 1
            or not isinstance(self.watch_id, str)
            or not self.watch_id
            or not isinstance(self.source_root, SemanticRootBinding)
            or not isinstance(
                self.candidate_snapshot, SemanticCandidateSnapshot
            )
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
    ) -> SubtitleAcquisitionPlanV2:
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

        plan = SubtitleAcquisitionPlanV2.create(
            run_id=request.run_id,
            config_revision=request.config_revision,
            config_revision_id=request.config_revision_id,
            watch_id=request.watch_id,
            created_at=request.created_at,
            source_root=request.source_root,
            source_folder=request.source_folder,
            folder_generation_id=request.folder_generation_id,
            inventory_id=request.inventory_id,
            candidate_snapshot=request.candidate_snapshot,
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
        try:
            current = NoFollowWatcher().scan_folder(
                AuthorizedRoot.create(
                    Path(request.source_root.path.as_posix())
                ),
                PurePosixPath(request.source_folder),
                logical_name=request.source_folder,
            )
        except Exception:
            raise _capability_error() from None
        if (
            current.semantic_inventory_id != request.inventory_id
            or current.candidates.semantic_snapshot
            != request.candidate_snapshot
        ):
            raise _capability_error()

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
