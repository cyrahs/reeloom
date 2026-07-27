from __future__ import annotations

import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from reeloom.adapters.filesystem import FilesystemScanner
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.manifest import (
    ExecutionManifest,
    ExecutionMove,
    ExecutionSource,
)
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.naming import filesystem_name_key
from reeloom.kernel.rename_plan import RootBinding
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.plans import PlanStore

_SUBTITLE_SAMPLE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class PreflightResult:
    plan_hash: str
    source_count: int
    move_count: int


@dataclass(frozen=True, slots=True)
class FilesystemPreflightExecutor:
    """Perform advisory read-only checks for one persisted plan."""

    plans: PlanStore

    def preflight(
        self,
        *,
        plan_hash: str,
    ) -> PreflightResult:
        manifest = self.load(plan_hash)
        self.validate(manifest)

        return PreflightResult(
            plan_hash=manifest.plan_hash,
            source_count=len(manifest.sources),
            move_count=len(manifest.moves),
        )

    def load(self, plan_hash: str) -> ExecutionManifest:
        return ExecutionManifest.from_canonical_bytes(
            self.plans.load(plan_hash),
            plan_hash=plan_hash,
        )

    def validate(self, manifest: ExecutionManifest) -> None:
        if manifest.source_root.device != manifest.output_root.device:
            raise ExecutorError(ExecutorErrorCode.CROSS_FILESYSTEM)

        source_fd = self._open_bound_root(manifest.source_root)
        output_fd: int | None = None
        try:
            output_fd = self._open_bound_root(manifest.output_root)
            if (
                os.fstat(source_fd).st_dev
                != os.fstat(output_fd).st_dev
            ):
                raise ExecutorError(
                    ExecutorErrorCode.CROSS_FILESYSTEM
                )
            for source in manifest.sources:
                self._check_source(source_fd, source)
            sources = {
                source.candidate_id: source
                for source in manifest.sources
            }
            if manifest.required_absent_directory is not None:
                self._check_required_absent_directory(
                    output_fd,
                    manifest.required_absent_directory,
                )
            for move in manifest.moves:
                source = sources[move.source_id]
                if source.device != manifest.output_root.device:
                    raise ExecutorError(
                        ExecutorErrorCode.CROSS_FILESYSTEM
                    )
                self._check_destination(output_fd, move, source)
        finally:
            if output_fd is not None:
                os.close(output_fd)
            os.close(source_fd)

    @staticmethod
    def _check_required_absent_directory(
        root_fd: int,
        directory: PurePosixPath,
    ) -> None:
        if directory.is_absolute() or len(directory.parts) != 1:
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        try:
            entries = os.listdir(root_fd)
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.PREFLIGHT_FAILED
            ) from None
        target = filesystem_name_key(directory.name)
        for entry in entries:
            if filesystem_name_key(entry) != target:
                continue
            try:
                metadata = os.stat(
                    entry,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except OSError:
                raise ExecutorError(
                    ExecutorErrorCode.PREFLIGHT_FAILED
                ) from None
            if stat.S_ISLNK(metadata.st_mode):
                raise ExecutorError(
                    ExecutorErrorCode.SYMLINK_NOT_ALLOWED
                )
            raise ExecutorError(
                ExecutorErrorCode.DESTINATION_COLLISION
            )

    @staticmethod
    def _open_bound_root(binding: RootBinding) -> int:
        try:
            root = AuthorizedRoot.create(Path(binding.path.as_posix()))
        except DomainError as error:
            if error.code is ErrorCode.SYMLINK_NOT_ALLOWED:
                raise ExecutorError(
                    ExecutorErrorCode.SYMLINK_NOT_ALLOWED
                ) from None
            raise ExecutorError(ExecutorErrorCode.ROOT_DRIFT) from None
        if root.device != binding.device or root.inode != binding.inode:
            raise ExecutorError(ExecutorErrorCode.ROOT_DRIFT)
        try:
            return FilesystemScanner._open_root(root)
        except (DomainError, OSError):
            raise ExecutorError(ExecutorErrorCode.ROOT_DRIFT) from None

    @classmethod
    def _check_source(
        cls,
        root_fd: int,
        source: ExecutionSource,
    ) -> None:
        current_fd = root_fd
        try:
            for part in source.relative_path.parts[:-1]:
                next_fd = cls._open_existing_directory(
                    current_fd,
                    part,
                    missing_code=ExecutorErrorCode.SOURCE_DRIFT,
                    nondirectory_code=ExecutorErrorCode.SOURCE_DRIFT,
                )
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd
            cls._check_source_file(
                current_fd,
                source.relative_path.name,
                source,
            )
        finally:
            if current_fd != root_fd:
                os.close(current_fd)

    @classmethod
    def _check_source_file(
        cls,
        parent_fd: int,
        name: str,
        source: ExecutionSource,
    ) -> None:
        file_fd: int | None = None
        try:
            before = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(before.st_mode):
                raise ExecutorError(
                    ExecutorErrorCode.SYMLINK_NOT_ALLOWED
                )
            if not cls._matches_source(before, source):
                raise ExecutorError(
                    ExecutorErrorCode.SOURCE_DRIFT
                )
            file_fd = os.open(
                name,
                cls._read_flags(),
                dir_fd=parent_fd,
            )
            opened = os.fstat(file_fd)
            if (
                not cls._same_identity(before, opened)
                or not cls._matches_source(opened, source)
            ):
                raise ExecutorError(
                    ExecutorErrorCode.SOURCE_DRIFT
                )
            if source.sample_digest is not None:
                digest = hashlib.sha256(
                    cls._read_prefix(
                        file_fd,
                        _SUBTITLE_SAMPLE_BYTES,
                    )
                ).hexdigest()
                if not hmac.compare_digest(
                    digest,
                    source.sample_digest,
                ):
                    raise ExecutorError(
                        ExecutorErrorCode.SOURCE_DRIFT
                    )
            if not cls._matches_source(os.fstat(file_fd), source):
                raise ExecutorError(
                    ExecutorErrorCode.SOURCE_DRIFT
                )
        except FileNotFoundError:
            raise ExecutorError(
                ExecutorErrorCode.SOURCE_DRIFT
            ) from None
        except ExecutorError:
            raise
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.PREFLIGHT_FAILED
            ) from None
        finally:
            if file_fd is not None:
                os.close(file_fd)

    @classmethod
    def _check_destination(
        cls,
        root_fd: int,
        move: ExecutionMove,
        source: ExecutionSource,
    ) -> None:
        current_fd = root_fd
        try:
            for part in move.destination.parts[:-1]:
                try:
                    next_fd = cls._open_existing_directory(
                        current_fd,
                        part,
                        missing_code=None,
                        nondirectory_code=(
                            ExecutorErrorCode.DESTINATION_COLLISION
                        ),
                    )
                except FileNotFoundError:
                    if os.fstat(current_fd).st_dev != source.device:
                        raise ExecutorError(
                            ExecutorErrorCode.CROSS_FILESYSTEM
                        ) from None
                    return
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd
            if os.fstat(current_fd).st_dev != source.device:
                raise ExecutorError(
                    ExecutorErrorCode.CROSS_FILESYSTEM
                )
            try:
                target = os.stat(
                    move.destination.name,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if stat.S_ISLNK(target.st_mode):
                raise ExecutorError(
                    ExecutorErrorCode.SYMLINK_NOT_ALLOWED
                )
            raise ExecutorError(
                ExecutorErrorCode.DESTINATION_COLLISION
            )
        except ExecutorError:
            raise
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.PREFLIGHT_FAILED
            ) from None
        finally:
            if current_fd != root_fd:
                os.close(current_fd)

    @staticmethod
    def _open_existing_directory(
        parent_fd: int,
        name: str,
        *,
        missing_code: ExecutorErrorCode | None,
        nondirectory_code: ExecutorErrorCode,
    ) -> int:
        try:
            expected = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if missing_code is None:
                raise
            raise ExecutorError(missing_code) from None
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.PREFLIGHT_FAILED
            ) from None
        if stat.S_ISLNK(expected.st_mode):
            raise ExecutorError(
                ExecutorErrorCode.SYMLINK_NOT_ALLOWED
            )
        if not stat.S_ISDIR(expected.st_mode):
            raise ExecutorError(nondirectory_code)

        directory_fd: int | None = None
        try:
            directory_fd = os.open(
                name,
                FilesystemPreflightExecutor._directory_flags(),
                dir_fd=parent_fd,
            )
            opened = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or expected.st_dev != opened.st_dev
                or expected.st_ino != opened.st_ino
            ):
                raise ExecutorError(
                    ExecutorErrorCode.PREFLIGHT_FAILED
                )
            return directory_fd
        except ExecutorError:
            if directory_fd is not None:
                os.close(directory_fd)
            raise
        except OSError:
            if directory_fd is not None:
                os.close(directory_fd)
            raise ExecutorError(
                ExecutorErrorCode.PREFLIGHT_FAILED
            ) from None

    @staticmethod
    def _read_flags() -> int:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise ExecutorError(
                ExecutorErrorCode.PREFLIGHT_FAILED
            )
        return (
            os.O_RDONLY
            | no_follow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )

    @staticmethod
    def _directory_flags() -> int:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory is None:
            raise ExecutorError(
                ExecutorErrorCode.PREFLIGHT_FAILED
            )
        return (
            os.O_RDONLY
            | no_follow
            | directory
            | getattr(os, "O_CLOEXEC", 0)
        )

    @staticmethod
    def _read_prefix(file_fd: int, limit: int) -> bytes:
        chunks: list[bytes] = []
        remaining = limit
        while remaining:
            chunk = os.read(file_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _matches_source(
        metadata: os.stat_result,
        source: ExecutionSource,
    ) -> bool:
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_size == source.size_bytes
            and metadata.st_dev == source.device
            and metadata.st_ino == source.inode
            and metadata.st_mtime_ns == source.mtime_ns
            and metadata.st_ctime_ns == source.ctime_ns
        )

    @staticmethod
    def _same_identity(
        first: os.stat_result,
        second: os.stat_result,
    ) -> bool:
        return (
            stat.S_ISREG(first.st_mode)
            and stat.S_ISREG(second.st_mode)
            and first.st_dev == second.st_dev
            and first.st_ino == second.st_ino
            and first.st_size == second.st_size
            and first.st_mtime_ns == second.st_mtime_ns
            and first.st_ctime_ns == second.st_ctime_ns
        )
