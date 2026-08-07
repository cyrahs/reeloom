from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import PurePosixPath

from reeloom.executor.atomic_rename import (
    AtomicRenameFailure,
    classify_atomic_rename_error,
    rename_noreplace,
)
from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.forward_execution import PathObservationState
from reeloom.kernel.naming import filesystem_name_key
from reeloom.kernel.semantic_identity import (
    SemanticRootBinding,
    SemanticSourceIdentity,
    validate_semantic_relative_path,
)
from reeloom.ports.forward_filesystem import (
    ForwardFilesystem,
    ForwardMoveDiagnostic,
    ForwardMoveEffect,
)


class _UnsafePath(RuntimeError):
    pass


class _CasefoldCollision(_UnsafePath):
    pass


def _diagnostic(error: OSError) -> ForwardMoveDiagnostic:
    category = classify_atomic_rename_error(error)
    return {
        AtomicRenameFailure.COLLISION: ForwardMoveDiagnostic.COLLISION,
        AtomicRenameFailure.CROSS_FILESYSTEM: (
            ForwardMoveDiagnostic.CROSS_FILESYSTEM
        ),
        AtomicRenameFailure.PERMISSION_DENIED: (
            ForwardMoveDiagnostic.PERMISSION_DENIED
        ),
        AtomicRenameFailure.TRANSIENT_IO: (
            ForwardMoveDiagnostic.TRANSIENT_IO
        ),
    }.get(category, ForwardMoveDiagnostic.UNKNOWN)


class PosixForwardFilesystem(ForwardFilesystem):
    """No-follow current-state adapter; persisted stat identity is unused."""

    @staticmethod
    def _open_root(root: SemanticRootBinding) -> int:
        try:
            observed = os.lstat(root.path.as_posix())
            if not stat.S_ISDIR(observed.st_mode):
                raise _UnsafePath
            no_follow = getattr(os, "O_NOFOLLOW", None)
            if no_follow is None:
                raise _UnsafePath
            descriptor = os.open(
                root.path.as_posix(),
                os.O_RDONLY
                | os.O_DIRECTORY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0),
            )
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != observed.st_dev
                or opened.st_ino != observed.st_ino
            ):
                os.close(descriptor)
                raise _UnsafePath
            return descriptor
        except _UnsafePath:
            raise
        except OSError:
            raise

    @staticmethod
    def _require_exact_name(parent_fd: int, name: str) -> None:
        target = filesystem_name_key(name)
        try:
            matches = tuple(
                item
                for item in os.listdir(parent_fd)
                if filesystem_name_key(item) == target
            )
        except OSError:
            raise
        if matches and any(item != name for item in matches):
            raise _CasefoldCollision

    @classmethod
    def _open_directory(cls, parent_fd: int, name: str) -> int:
        cls._require_exact_name(parent_fd, name)
        try:
            observed = os.stat(
                name, dir_fd=parent_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            raise
        if not stat.S_ISDIR(observed.st_mode):
            raise _UnsafePath
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise _UnsafePath
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | no_follow
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != observed.st_dev
            or opened.st_ino != observed.st_ino
        ):
            os.close(descriptor)
            raise _UnsafePath
        return descriptor

    @classmethod
    def _open_parent(
        cls,
        root_fd: int,
        relative_path: PurePosixPath,
        *,
        create: bool,
    ) -> int:
        current = os.dup(root_fd)
        try:
            for part in relative_path.parts[:-1]:
                try:
                    next_fd = cls._open_directory(current, part)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o755, dir_fd=current)
                    next_fd = cls._open_directory(current, part)
                os.close(current)
                current = next_fd
            return current
        except Exception:
            os.close(current)
            raise

    @staticmethod
    def _matches_file(
        file_fd: int,
        expected: SemanticSourceIdentity,
    ) -> bool:
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != expected.size_bytes
        ):
            return False
        if expected.kind is CandidateKind.VIDEO:
            after = os.fstat(file_fd)
            return (
                stat.S_ISREG(after.st_mode)
                and after.st_size == expected.size_bytes
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(file_fd)
        return (
            stat.S_ISREG(after.st_mode)
            and after.st_size == expected.size_bytes
            and digest.hexdigest() == expected.sha256
        )

    def observe(
        self,
        *,
        root: SemanticRootBinding,
        relative_path: PurePosixPath,
        expected: SemanticSourceIdentity,
    ) -> PathObservationState:
        try:
            validate_semantic_relative_path(relative_path)
            root_fd = self._open_root(root)
        except _UnsafePath:
            return PathObservationState.UNSAFE
        except OSError:
            return PathObservationState.UNAVAILABLE
        parent_fd: int | None = None
        file_fd: int | None = None
        try:
            try:
                parent_fd = self._open_parent(
                    root_fd, relative_path, create=False
                )
            except FileNotFoundError:
                return PathObservationState.ABSENT
            try:
                self._require_exact_name(
                    parent_fd, relative_path.name
                )
                metadata = os.stat(
                    relative_path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return PathObservationState.ABSENT
            if not stat.S_ISREG(metadata.st_mode):
                return PathObservationState.UNSAFE
            no_follow = getattr(os, "O_NOFOLLOW", None)
            if no_follow is None:
                return PathObservationState.UNSAFE
            file_fd = os.open(
                relative_path.name,
                os.O_RDONLY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
            return (
                PathObservationState.MATCHING
                if self._matches_file(file_fd, expected)
                else PathObservationState.MISMATCHED
            )
        except _CasefoldCollision:
            return PathObservationState.MISMATCHED
        except _UnsafePath:
            return PathObservationState.UNSAFE
        except OSError:
            return PathObservationState.UNAVAILABLE
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if parent_fd is not None:
                os.close(parent_fd)
            os.close(root_fd)

    def move(
        self,
        *,
        source_root: SemanticRootBinding,
        source_path: PurePosixPath,
        expected: SemanticSourceIdentity,
        destination_root: SemanticRootBinding,
        destination_path: PurePosixPath,
    ) -> ForwardMoveEffect:
        source_root_fd: int | None = None
        destination_root_fd: int | None = None
        try:
            validate_semantic_relative_path(source_path)
            validate_semantic_relative_path(destination_path)
            source_root_fd = self._open_root(source_root)
            destination_root_fd = self._open_root(destination_root)
        except _UnsafePath:
            if source_root_fd is not None:
                os.close(source_root_fd)
            return ForwardMoveEffect(ForwardMoveDiagnostic.UNSAFE)
        except OSError as error:
            if source_root_fd is not None:
                os.close(source_root_fd)
            return ForwardMoveEffect(_diagnostic(error))
        source_parent_fd: int | None = None
        destination_parent_fd: int | None = None
        warnings: list[str] = []
        try:
            source_parent_fd = self._open_parent(
                source_root_fd, source_path, create=False
            )
            destination_parent_fd = self._open_parent(
                destination_root_fd, destination_path, create=True
            )
            self._require_exact_name(source_parent_fd, source_path.name)
            try:
                self._require_exact_name(
                    destination_parent_fd, destination_path.name
                )
            except _CasefoldCollision:
                return ForwardMoveEffect(
                    ForwardMoveDiagnostic.COLLISION
                )
            no_follow = getattr(os, "O_NOFOLLOW", None)
            if no_follow is None:
                raise _UnsafePath
            source_fd = os.open(
                source_path.name,
                os.O_RDONLY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=source_parent_fd,
            )
            try:
                if not self._matches_file(source_fd, expected):
                    return ForwardMoveEffect(
                        ForwardMoveDiagnostic.TRANSIENT_IO
                    )
            finally:
                os.close(source_fd)
            try:
                os.stat(
                    destination_path.name,
                    dir_fd=destination_parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                return ForwardMoveEffect(
                    ForwardMoveDiagnostic.COLLISION
                )
            try:
                rename_noreplace(
                    source_parent_fd,
                    source_path.name,
                    destination_parent_fd,
                    destination_path.name,
                )
                diagnostic = ForwardMoveDiagnostic.NATIVE
            except OSError as error:
                if (
                    classify_atomic_rename_error(error)
                    is not AtomicRenameFailure.UNSUPPORTED
                ):
                    return ForwardMoveEffect(_diagnostic(error))
                try:
                    os.stat(
                        destination_path.name,
                        dir_fd=destination_parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    return ForwardMoveEffect(
                        ForwardMoveDiagnostic.COLLISION
                    )
                try:
                    os.rename(
                        source_path.name,
                        destination_path.name,
                        src_dir_fd=source_parent_fd,
                        dst_dir_fd=destination_parent_fd,
                    )
                    diagnostic = ForwardMoveDiagnostic.CHECKED_RENAME
                except OSError as fallback_error:
                    return ForwardMoveEffect(_diagnostic(fallback_error))
            for descriptor in (source_parent_fd, destination_parent_fd):
                try:
                    os.fsync(descriptor)
                except OSError as error:
                    if error.errno in {
                        errno.EINVAL,
                        errno.ENOSYS,
                        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
                        errno.EOPNOTSUPP,
                    }:
                        warnings.append("directory_fsync_unsupported")
                    else:
                        warnings.append("directory_fsync_failed")
            return ForwardMoveEffect(diagnostic, tuple(sorted(set(warnings))))
        except FileNotFoundError:
            return ForwardMoveEffect(ForwardMoveDiagnostic.TRANSIENT_IO)
        except _CasefoldCollision:
            return ForwardMoveEffect(ForwardMoveDiagnostic.COLLISION)
        except _UnsafePath:
            return ForwardMoveEffect(ForwardMoveDiagnostic.UNSAFE)
        except OSError as error:
            return ForwardMoveEffect(_diagnostic(error))
        finally:
            for descriptor in (
                destination_parent_fd,
                source_parent_fd,
                destination_root_fd,
                source_root_fd,
            ):
                if descriptor is not None:
                    os.close(descriptor)
