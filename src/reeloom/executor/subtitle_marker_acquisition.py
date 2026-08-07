from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.subtitle_publication import (
    SubtitleMarkerPublisher,
    SubtitlePublicationResult,
)
from reeloom.kernel.approval import ApprovalScope
from reeloom.kernel.errors import DomainError
from reeloom.kernel.naming import filesystem_name_key
from reeloom.kernel.subtitle_acquisition import (
    CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION,
    CURRENT_SUBTITLE_SEARCH_PARSER_VERSION,
    CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION,
    PlannedSubtitleMember,
    SubtitleAcquisitionPlan,
    SubtitleArchiveSetCapability,
)
from reeloom.kernel.subtitle_publication import (
    SubtitlePublicationManifest,
    SubtitlePublicationMember,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.approvals import ApprovalStore
from reeloom.ports.subtitle_acquisition import (
    DownloadedSubtitleArchiveSet,
    SubtitleAcquisitionPlanStore,
    SubtitleArchiveCache,
    SubtitleArchiveError,
    SubtitleArchiveFetcher,
    SubtitleArchiveInspector,
)


@dataclass(slots=True)
class _CachedMemberSource:
    inspector: SubtitleArchiveInspector
    members: dict[str, PlannedSubtitleMember]
    archives: dict[object, DownloadedSubtitleArchiveSet]

    async def read_member(
        self,
        member: SubtitlePublicationMember,
    ) -> bytes:
        planned = self.members.get(member.name)
        if planned is None:
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        archive = self.archives.get(planned.archive_set_id)
        if archive is None:
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        try:
            return await self.inspector.extract_member(archive, planned)
        except SubtitleArchiveError as error:
            raise ExecutorError(
                ExecutorErrorCode.TRANSIENT_IO
                if error.retryable
                else ExecutorErrorCode.SOURCE_DRIFT
            ) from None


@dataclass(frozen=True, slots=True)
class SubtitleMarkerAcquisitionExecutor:
    """Journal-free subtitle acquisition that reconciles current bytes."""

    plans: SubtitleAcquisitionPlanStore
    approvals: ApprovalStore
    cache: SubtitleArchiveCache
    fetcher: SubtitleArchiveFetcher
    inspector: SubtitleArchiveInspector
    publisher: SubtitleMarkerPublisher = SubtitleMarkerPublisher()

    async def apply(
        self,
        *,
        plan_hash: str,
        approval_id: str,
    ) -> SubtitlePublicationResult:
        plan = self._load(plan_hash)
        self.approvals.claim(
            approval_id=approval_id,
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
            scope=ApprovalScope.SUBTITLE_ACQUIRE,
        )
        return await self._execute(plan)

    async def reconcile(
        self,
        *,
        plan_hash: str,
        approval_id: str,
    ) -> SubtitlePublicationResult:
        plan = self._load(plan_hash)
        self.approvals.require_claim(
            approval_id=approval_id,
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
            scope=ApprovalScope.SUBTITLE_ACQUIRE,
        )
        return await self._execute(plan)

    def _load(self, plan_hash: str) -> SubtitleAcquisitionPlan:
        try:
            return SubtitleAcquisitionPlan.from_canonical_bytes(
                self.plans.load(plan_hash),
                plan_hash=plan_hash,
            )
        except ExecutorError:
            raise
        except (DomainError, TypeError, ValueError):
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN) from None

    async def _execute(
        self,
        plan: SubtitleAcquisitionPlan,
    ) -> SubtitlePublicationResult:
        if (
            self.fetcher.provider_version != plan.provider_version
            or self.fetcher.parser_version != plan.parser_version
            or self.inspector.inspector_version != plan.inspector_version
            or plan.provider_version
            != CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION
            or plan.parser_version != CURRENT_SUBTITLE_SEARCH_PARSER_VERSION
            or plan.inspector_version
            != CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        source_root = Path(plan.source_root.path.as_posix())
        self._require_disjoint(source_root, self.fetcher.workspace_root)
        self._require_disjoint(source_root, self.cache.cache_root)
        try:
            root = AuthorizedRoot.create(source_root)
        except DomainError:
            raise ExecutorError(ExecutorErrorCode.ROOT_DRIFT) from None
        archives = await self._load_archives(plan)
        source = _CachedMemberSource(
            inspector=self.inspector,
            members={item.destination_name: item for item in plan.members},
            archives={item.capability.archive_set_id: item for item in archives},
        )
        return await self.publisher.publish(
            root=root,
            source_folder=plan.source_folder,
            manifest=SubtitlePublicationManifest.from_plan(plan),
            content_source=source,
        )

    async def _load_archives(
        self,
        plan: SubtitleAcquisitionPlan,
    ) -> tuple[DownloadedSubtitleArchiveSet, ...]:
        downloaded_sets: list[DownloadedSubtitleArchiveSet] = []
        planned_rejected = set(plan.rejected_entries)
        for source in plan.archives:
            capability = SubtitleArchiveSetCapability(
                source.archive_set_id,
                source.release_id,
                source.format,
                source.thread_id,
                source.post_id,
                tuple(item.attachment_id for item in source.volumes),
                sum(item.size_bytes for item in source.volumes),
            )
            try:
                downloaded = self.cache.load(capability, source.volumes)
                if downloaded is None:
                    fetched = await self.fetcher.fetch(capability)
                    if (
                        fetched.capability != capability
                        or tuple(item.volume for item in fetched.volumes)
                        != source.volumes
                    ):
                        raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
                    downloaded = self.cache.store(fetched)
                inspected = await self.inspector.inspect(
                    downloaded,
                    season_numbers=source.season_numbers,
                )
            except ExecutorError:
                raise
            except SubtitleArchiveError as error:
                raise ExecutorError(
                    ExecutorErrorCode.TRANSIENT_IO
                    if error.retryable
                    else ExecutorErrorCode.SOURCE_DRIFT
                ) from None
            actual_members = tuple(
                sorted(
                    (
                        PlannedSubtitleMember.from_inspected(item)
                        for item in inspected.members
                    ),
                    key=lambda item: (
                        item.archive_set_id,
                        filesystem_name_key(item.source_path.as_posix()),
                        item.source_path.as_posix(),
                    ),
                )
            )
            expected_members = tuple(
                item
                for item in plan.members
                if item.archive_set_id == source.archive_set_id
            )
            if (
                inspected.source != source
                or actual_members != expected_members
                or set(inspected.rejected_entries)
                != {
                    item
                    for item in planned_rejected
                    if item.archive_set_id == source.archive_set_id
                }
            ):
                raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
            downloaded_sets.append(downloaded)
        return tuple(downloaded_sets)

    @staticmethod
    def _require_disjoint(source_root: Path, other_root: Path) -> None:
        if not source_root.is_absolute() or not other_root.is_absolute():
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        try:
            common = Path(os.path.commonpath((source_root, other_root)))
        except ValueError:
            return
        if common in {source_root, other_root}:
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
