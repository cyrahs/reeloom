from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath

from reeloom.adapters.subtitle_journal import (
    FilesystemSubtitleAcquisitionJournalStore,
)
from reeloom.executor.atomic_rename import rename_noreplace_compatible
from reeloom.executor.errors import (
    ExecutorError,
    ExecutorErrorCode,
    atomic_move_error_code,
    filesystem_error_code,
)
from reeloom.executor.subtitle_transaction import (
    SubtitleAcquisitionTransactionRecord,
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
    SUBTITLE_PUBLICATION_MARKER,
    SubtitlePublicationManifest,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.approvals import ApprovalStore
from reeloom.ports.subtitle_acquisition import (
    DownloadedSubtitleArchiveSet,
    SubtitleAcquisitionPlanStore,
    SubtitleArchiveError,
    SubtitleArchiveFetcher,
    SubtitleArchiveInspector,
)
from reeloom.server.watcher import NoFollowWatcher

_rename_noreplace = rename_noreplace_compatible


def _collision(
    *,
    stage: str,
    reason: str,
    **details: object,
) -> ExecutorError:
    return ExecutorError(
        ExecutorErrorCode.DESTINATION_COLLISION,
        context={"stage": stage, "reason": reason, **details},
    )


def _safe_staging_mode(mode: int) -> bool:
    """Require owner rwx and prohibit writes by group or other."""

    return mode & 0o700 == 0o700 and mode & 0o022 == 0


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionResult:
    run_id: str
    plan_hash: str
    approval_id: str
    transaction_id: str
    destination_name: str
    destination_device: int
    destination_inode: int
    published_count: int
    status: str = "completed"


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionExecutor:
    plans: SubtitleAcquisitionPlanStore
    approvals: ApprovalStore
    journals: FilesystemSubtitleAcquisitionJournalStore
    fetcher: SubtitleArchiveFetcher
    inspector: SubtitleArchiveInspector
    watcher: NoFollowWatcher = NoFollowWatcher()

    async def apply(
        self,
        *,
        plan_hash: str,
        approval_id: str,
    ) -> SubtitleAcquisitionResult:
        plan = self._load(plan_hash)
        transaction = SubtitleAcquisitionTransactionRecord.create(
            plan,
            approval_id=approval_id,
        )
        with self.journals.transaction_lock(transaction):
            self.journals.begin(transaction)
            self.approvals.claim(
                approval_id=approval_id,
                run_id=plan.run_id,
                plan_hash=plan.plan_hash,
                scope=ApprovalScope.SUBTITLE_ACQUIRE,
            )
            self.journals.record(transaction, "approval_claimed")
            return await self._resume(plan, transaction)

    async def recover(
        self,
        *,
        plan_hash: str,
        approval_id: str,
    ) -> SubtitleAcquisitionResult:
        plan = self._load(plan_hash)
        transaction = SubtitleAcquisitionTransactionRecord.create(
            plan,
            approval_id=approval_id,
        )
        with self.journals.transaction_lock(transaction):
            self.journals.begin(transaction)
            self.approvals.require_claim(
                approval_id=approval_id,
                run_id=plan.run_id,
                plan_hash=plan.plan_hash,
                scope=ApprovalScope.SUBTITLE_ACQUIRE,
            )
            self.journals.record(transaction, "approval_claimed")
            return await self._resume(plan, transaction)

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

    async def _resume(
        self,
        plan: SubtitleAcquisitionPlan,
        transaction: SubtitleAcquisitionTransactionRecord,
    ) -> SubtitleAcquisitionResult:
        root, root_fd = self._open_source_root(plan)
        try:
            source_fd = self._open_source_folder(root_fd, plan)
            try:
                return await self._resume_in_source(
                    plan,
                    transaction,
                    root_fd=root_fd,
                    source_fd=source_fd,
                )
            finally:
                os.close(source_fd)
        finally:
            os.close(root_fd)

    async def _resume_in_source(
        self,
        plan: SubtitleAcquisitionPlan,
        transaction: SubtitleAcquisitionTransactionRecord,
        *,
        root_fd: int,
        source_fd: int,
    ) -> SubtitleAcquisitionResult:
        try:
            identity = self.journals.staging_identity(transaction)
            publish_started = self.journals.has(
                transaction, "publish_started"
            )
            published = self.journals.has(transaction, "published")
            completed = self.journals.has(transaction, "completed")
            if completed and not published:
                raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
            if identity is not None and (publish_started or published):
                if self._is_valid_published_directory(
                    source_fd,
                    transaction,
                    plan,
                    identity=identity,
                ):
                    self.journals.record(transaction, "published")
                    self.journals.record(transaction, "completed")
                    return self._result(
                        plan, transaction, destination_identity=identity
                    )
            if (
                self._name_state(source_fd, transaction.destination_name)
                != "absent"
            ):
                raise _collision(
                    stage="destination_preflight",
                    reason="name_exists",
                )
            if completed or published:
                raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED)

            self._require_candidate_snapshot(plan)
            downloaded = await self._refetch_and_verify(plan)
            self.journals.record(transaction, "downloads_verified")
            staging_fd, identity = self._open_or_create_staging(
                source_fd,
                plan,
                transaction,
            )
            try:
                self._require_only_planned_names(staging_fd, plan)
                by_archive = {
                    item.capability.archive_set_id: item
                    for item in downloaded
                }
                for index, member in enumerate(plan.members):
                    archive = by_archive.get(member.archive_set_id)
                    if archive is None:
                        raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
                    await self._write_or_verify_member(
                        staging_fd,
                        transaction,
                        index,
                        member,
                        archive,
                    )
                self._write_complete_marker(staging_fd, plan)
                self._verify_directory(staging_fd, plan)
                try:
                    os.fsync(staging_fd)
                except OSError as error:
                    raise ExecutorError(
                        filesystem_error_code(
                            error,
                            default=ExecutorErrorCode.RECOVERY_REQUIRED,
                        )
                    ) from None
            finally:
                os.close(staging_fd)

            self._require_source_folder(root_fd, plan)
            self._require_candidate_snapshot(plan)
            self.journals.record(transaction, "publish_started")
            failure_code: ExecutorErrorCode | None = None
            try:
                _rename_noreplace(
                    source_fd,
                    transaction.staging_name,
                    source_fd,
                    transaction.destination_name,
                )
            except OSError as error:
                failure_code = atomic_move_error_code(error)
            if not self._is_valid_published_directory(
                source_fd,
                transaction,
                plan,
                identity=identity,
            ):
                if failure_code is not None:
                    if failure_code is ExecutorErrorCode.DESTINATION_COLLISION:
                        raise _collision(
                            stage="publish",
                            reason="name_exists",
                        )
                    raise ExecutorError(failure_code)
                raise ExecutorError(ExecutorErrorCode.STATE_AMBIGUOUS)
            try:
                os.fsync(source_fd)
            except OSError:
                raise ExecutorError(
                    ExecutorErrorCode.RECOVERY_REQUIRED
                ) from None
            self.journals.record(transaction, "published")
            self.journals.record(transaction, "completed")
            return self._result(
                plan, transaction, destination_identity=identity
            )
        except ExecutorError:
            raise

    async def _refetch_and_verify(
        self,
        plan: SubtitleAcquisitionPlan,
    ) -> tuple[DownloadedSubtitleArchiveSet, ...]:
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
        self._require_disjoint_workspace(plan)
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
                downloaded = await self.fetcher.fetch(capability)
                if (
                    downloaded.capability != capability
                    or tuple(item.volume for item in downloaded.volumes)
                    != source.volumes
                ):
                    raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
                inspected = await self.inspector.inspect(
                    downloaded,
                    season_numbers=source.season_numbers,
                )
            except SubtitleArchiveError as error:
                raise ExecutorError(
                    ExecutorErrorCode.TRANSIENT_IO
                    if error.retryable
                    else ExecutorErrorCode.SOURCE_DRIFT
                ) from None
            if inspected.source != source:
                raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
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
                actual_members != expected_members
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

    def _open_or_create_staging(
        self,
        root_fd: int,
        plan: SubtitleAcquisitionPlan,
        transaction: SubtitleAcquisitionTransactionRecord,
    ) -> tuple[int, tuple[int, int]]:
        identity = self.journals.staging_identity(transaction)
        started = self.journals.has(
            transaction, "staging_create_started"
        )
        created_this_attempt = False
        if identity is None and not started:
            if self._name_state(root_fd, transaction.staging_name) != "absent":
                raise _collision(
                    stage="staging_prepare",
                    reason="name_exists",
                )
            self.journals.record(transaction, "staging_create_started")
            try:
                os.mkdir(transaction.staging_name, 0o700, dir_fd=root_fd)
                created_this_attempt = True
            except OSError as error:
                code = filesystem_error_code(
                    error,
                    default=ExecutorErrorCode.DESTINATION_COLLISION,
                )
                if code is ExecutorErrorCode.DESTINATION_COLLISION:
                    raise _collision(
                        stage="staging_prepare",
                        reason="create_failed",
                    ) from None
                raise ExecutorError(code) from None
        elif identity is None:
            state = self._name_state(root_fd, transaction.staging_name)
            if state == "absent":
                try:
                    os.mkdir(transaction.staging_name, 0o700, dir_fd=root_fd)
                    created_this_attempt = True
                except OSError as error:
                    code = filesystem_error_code(
                        error,
                        default=ExecutorErrorCode.DESTINATION_COLLISION,
                    )
                    if code is ExecutorErrorCode.DESTINATION_COLLISION:
                        raise _collision(
                            stage="staging_prepare",
                            reason="create_failed",
                        ) from None
                    raise ExecutorError(code) from None
            elif state != "directory":
                raise _collision(
                    stage="staging_prepare",
                    reason="entry_type_mismatch",
                )
        staging_fd = self._open_directory(root_fd, transaction.staging_name)
        metadata = os.fstat(staging_fd)
        actual_identity = (metadata.st_dev, metadata.st_ino)
        if identity is not None and actual_identity != identity:
            os.close(staging_fd)
            raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
        if identity is None:
            mode = stat.S_IMODE(metadata.st_mode)
            if not _safe_staging_mode(mode):
                os.close(staging_fd)
                raise _collision(
                    stage="staging_validate",
                    reason="unsafe_permissions",
                    actual_mode=mode,
                    expected_policy="owner_rwx_no_group_or_other_write",
                )
            if metadata.st_uid != os.geteuid():
                os.close(staging_fd)
                raise _collision(
                    stage="staging_validate",
                    reason="owner_mismatch",
                    actual_uid=metadata.st_uid,
                    expected_uid=os.geteuid(),
                )
            if not created_this_attempt:
                entries = os.listdir(staging_fd)
                if entries:
                    os.close(staging_fd)
                    raise _collision(
                        stage="staging_validate",
                        reason="not_empty",
                        entry_count=len(entries),
                    )
            self.journals.record_staging(
                transaction,
                device=metadata.st_dev,
                inode=metadata.st_ino,
            )
        return staging_fd, actual_identity

    async def _write_or_verify_member(
        self,
        staging_fd: int,
        transaction: SubtitleAcquisitionTransactionRecord,
        index: int,
        member: PlannedSubtitleMember,
        downloaded: DownloadedSubtitleArchiveSet,
    ) -> None:
        state = self._name_state(staging_fd, member.destination_name)
        if state != "absent":
            if state != "file" or not self._file_matches(
                staging_fd, member
            ):
                raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED)
            self.journals.record_member(transaction, index)
            return
        try:
            content = await self.inspector.extract_member(downloaded, member)
        except SubtitleArchiveError as error:
            raise ExecutorError(
                ExecutorErrorCode.TRANSIENT_IO
                if error.retryable
                else ExecutorErrorCode.SOURCE_DRIFT
            ) from None
        if (
            len(content) != member.size_bytes
            or hashlib.sha256(content).hexdigest() != member.sha256
        ):
            raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                member.destination_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=staging_fd,
            )
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError
                remaining = remaining[written:]
            try:
                os.fsync(descriptor)
            except OSError:
                # This is only a compatibility bridge for the legacy v1
                # publisher. Current bytes and the marker are authoritative.
                pass
        except FileExistsError:
            raise _collision(
                stage="member_write",
                reason="name_exists",
                member_index=index,
            ) from None
        except OSError as error:
            raise ExecutorError(
                filesystem_error_code(
                    error,
                    default=ExecutorErrorCode.RECOVERY_REQUIRED,
                )
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if not self._file_matches(staging_fd, member):
            raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED)
        self.journals.record_member(transaction, index)

    @classmethod
    def _verify_directory(
        cls,
        directory_fd: int,
        plan: SubtitleAcquisitionPlan,
    ) -> None:
        cls._require_only_planned_names(directory_fd, plan)
        if any(
            not cls._file_matches(directory_fd, member)
            for member in plan.members
        ):
            raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED)
        marker_state = cls._complete_marker_state(directory_fd, plan)
        if marker_state not in {"absent", "matching"}:
            raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED)

    @staticmethod
    def _require_only_planned_names(
        directory_fd: int,
        plan: SubtitleAcquisitionPlan,
    ) -> None:
        expected = {item.destination_name for item in plan.members} | {
            SUBTITLE_PUBLICATION_MARKER
        }
        expected_keys = {filesystem_name_key(item) for item in expected}
        try:
            names = set(os.listdir(directory_fd))
        except OSError:
            raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED) from None
        if any(
            name not in expected
            or filesystem_name_key(name) not in expected_keys
            for name in names
        ):
            raise _collision(
                stage="staging_validate",
                reason="unexpected_entries",
                entry_count=len(names),
            )
        if len({filesystem_name_key(name) for name in names}) != len(names):
            raise _collision(
                stage="staging_validate",
                reason="casefold_collision",
                entry_count=len(names),
            )

    @staticmethod
    def _file_matches(
        directory_fd: int,
        member: PlannedSubtitleMember,
    ) -> bool:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                member.destination_name,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size != member.size_bytes
            ):
                return False
            digest = hashlib.sha256()
            remaining = member.size_bytes
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    return False
                digest.update(chunk)
                remaining -= len(chunk)
            return (
                not os.read(descriptor, 1)
                and digest.hexdigest() == member.sha256
            )
        except OSError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @classmethod
    def _is_valid_published_directory(
        cls,
        root_fd: int,
        transaction: SubtitleAcquisitionTransactionRecord,
        plan: SubtitleAcquisitionPlan,
        *,
        identity: tuple[int, int],
    ) -> bool:
        if cls._name_state(root_fd, transaction.staging_name) != "absent":
            return False
        try:
            destination_fd = cls._open_directory(
                root_fd, transaction.destination_name
            )
        except ExecutorError:
            return False
        try:
            metadata = os.fstat(destination_fd)
            if (metadata.st_dev, metadata.st_ino) != identity:
                return False
            cls._verify_directory(destination_fd, plan)
            cls._write_complete_marker(destination_fd, plan)
            return True
        except ExecutorError:
            return False
        finally:
            os.close(destination_fd)

    @classmethod
    def _write_complete_marker(
        cls,
        directory_fd: int,
        plan: SubtitleAcquisitionPlan,
    ) -> None:
        state = cls._complete_marker_state(directory_fd, plan)
        if state == "matching":
            return
        if state != "absent":
            raise _collision(
                stage="complete_marker",
                reason="marker_mismatch",
            )
        content = SubtitlePublicationManifest.from_plan(plan).canonical_bytes()
        descriptor: int | None = None
        try:
            descriptor = os.open(
                SUBTITLE_PUBLICATION_MARKER,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError
                remaining = remaining[written:]
            os.fsync(descriptor)
        except FileExistsError:
            if cls._complete_marker_state(directory_fd, plan) != "matching":
                raise _collision(
                    stage="complete_marker",
                    reason="marker_race",
                ) from None
        except OSError as error:
            raise ExecutorError(
                filesystem_error_code(
                    error,
                    default=ExecutorErrorCode.RECOVERY_REQUIRED,
                )
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _complete_marker_state(
        directory_fd: int,
        plan: SubtitleAcquisitionPlan,
    ) -> str:
        expected = SubtitlePublicationManifest.from_plan(plan).canonical_bytes()
        descriptor: int | None = None
        try:
            descriptor = os.open(
                SUBTITLE_PUBLICATION_MARKER,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(
                expected
            ):
                return "mismatch"
            content = bytearray()
            while len(content) < len(expected):
                chunk = os.read(descriptor, len(expected) - len(content))
                if not chunk:
                    break
                content.extend(chunk)
            if os.read(descriptor, 1):
                return "mismatch"
            return "matching" if bytes(content) == expected else "mismatch"
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "unsafe"
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _name_state(parent_fd: int, name: str) -> str:
        target_key = filesystem_name_key(name)
        try:
            matches = [
                item
                for item in os.listdir(parent_fd)
                if filesystem_name_key(item) == target_key
            ]
        except OSError:
            raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED) from None
        if not matches:
            return "absent"
        if matches != [name]:
            return "collision"
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return "collision"
        if stat.S_ISDIR(metadata.st_mode):
            return "directory"
        if stat.S_ISREG(metadata.st_mode):
            return "file"
        return "collision"

    @staticmethod
    def _open_directory(parent_fd: int, name: str) -> int:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise OSError
            return descriptor
        except OSError:
            raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED) from None

    @staticmethod
    def _open_source_folder(
        root_fd: int,
        plan: SubtitleAcquisitionPlan,
    ) -> int:
        try:
            descriptor = SubtitleAcquisitionExecutor._open_directory(
                root_fd, plan.source_folder
            )
        except ExecutorError:
            raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT) from None
        try:
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != (
                plan.source_folder_device,
                plan.source_folder_inode,
            ):
                raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _require_source_folder(
        root_fd: int,
        plan: SubtitleAcquisitionPlan,
    ) -> None:
        descriptor = SubtitleAcquisitionExecutor._open_source_folder(
            root_fd, plan
        )
        os.close(descriptor)

    @staticmethod
    def _open_source_root(
        plan: SubtitleAcquisitionPlan,
    ) -> tuple[AuthorizedRoot, int]:
        try:
            root = AuthorizedRoot.create(Path(plan.source_root.path.as_posix()))
            if (root.device, root.inode) != (
                plan.source_root.device,
                plan.source_root.inode,
            ):
                raise ExecutorError(ExecutorErrorCode.ROOT_DRIFT)
            descriptor = os.open(
                root.path,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
            )
            return root, descriptor
        except ExecutorError:
            raise
        except Exception:
            raise ExecutorError(ExecutorErrorCode.ROOT_DRIFT) from None

    def _require_candidate_snapshot(
        self,
        plan: SubtitleAcquisitionPlan,
    ) -> None:
        try:
            root = AuthorizedRoot.create(
                Path(plan.source_root.path.as_posix())
            )
            snapshot = self.watcher.scan_folder(
                root,
                PurePosixPath(plan.source_folder),
                logical_name=plan.source_folder,
            )
            if (
                (snapshot.device, snapshot.inode)
                != (
                    plan.source_folder_device,
                    plan.source_folder_inode,
                )
                or snapshot.candidates.snapshot_id
                != plan.candidate_snapshot_id
            ):
                raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
        except ExecutorError:
            raise
        except Exception:
            raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT) from None

    def _require_disjoint_workspace(
        self,
        plan: SubtitleAcquisitionPlan,
    ) -> None:
        source = Path(plan.source_root.path.as_posix())
        workspace = self.fetcher.workspace_root
        if not workspace.is_absolute():
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        try:
            common = Path(os.path.commonpath((source, workspace)))
        except ValueError:
            return
        if common in {source, workspace}:
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)

    @staticmethod
    def _result(
        plan: SubtitleAcquisitionPlan,
        transaction: SubtitleAcquisitionTransactionRecord,
        *,
        destination_identity: tuple[int, int],
    ) -> SubtitleAcquisitionResult:
        return SubtitleAcquisitionResult(
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
            approval_id=transaction.approval_id,
            transaction_id=transaction.transaction_id,
            destination_name=transaction.destination_name,
            destination_device=destination_identity[0],
            destination_inode=destination_identity[1],
            published_count=len(plan.members),
        )
