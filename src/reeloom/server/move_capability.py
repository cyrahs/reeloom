from __future__ import annotations

import os
import stat
import uuid
from dataclasses import dataclass
from enum import StrEnum

from reeloom.executor.atomic_rename import rename_noreplace
from reeloom.executor.errors import (
    ExecutorErrorCode,
    atomic_move_error_code,
)
from reeloom.policy.path_policy import AuthorizedRoot


class MoveCapabilityStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CROSS_FILESYSTEM = "cross_filesystem"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class MoveCapability:
    status: MoveCapabilityStatus
    failure_code: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "failure_code": self.failure_code,
        }


def probe_move_capability(
    source_root: AuthorizedRoot,
    destination_root: AuthorizedRoot,
) -> MoveCapability:
    """Probe strict no-replace semantics using only owned empty directories."""

    source_fd = _open_root(source_root)
    destination_fd = _open_root(destination_root)
    destination_parent_fd: int | None = None
    token = uuid.uuid4().hex
    source_name = f".reeloom-move-probe-source-{token}"
    collision_source = f".reeloom-move-probe-collision-{token}"
    parent_name = f".reeloom-move-probe-target-{token}"
    target_name = "moved"
    collision_target = "occupied"
    owned: list[tuple[int, str, tuple[int, int]]] = []
    try:
        source_identity = _mkdir_owned(source_fd, source_name)
        owned.append((source_fd, source_name, source_identity))
        collision_source_identity = _mkdir_owned(
            source_fd, collision_source
        )
        owned.append(
            (source_fd, collision_source, collision_source_identity)
        )
        parent_identity = _mkdir_owned(destination_fd, parent_name)
        owned.append((destination_fd, parent_name, parent_identity))
        destination_parent_fd = _open_owned_directory(
            destination_fd,
            parent_name,
            parent_identity,
        )
        collision_target_identity = _mkdir_owned(
            destination_parent_fd,
            collision_target,
        )
        owned.append(
            (
                destination_parent_fd,
                collision_target,
                collision_target_identity,
            )
        )
        try:
            rename_noreplace(
                source_fd,
                source_name,
                destination_parent_fd,
                target_name,
            )
        except OSError as error:
            return _failure(atomic_move_error_code(error))
        if (
            _state(source_fd, source_name, source_identity) != "absent"
            or _state(
                destination_parent_fd,
                target_name,
                source_identity,
            )
            != "expected"
        ):
            return MoveCapability(MoveCapabilityStatus.UNCERTAIN)
        owned.pop(0)
        owned.append(
            (
                destination_parent_fd,
                target_name,
                source_identity,
            )
        )
        try:
            rename_noreplace(
                source_fd,
                collision_source,
                destination_parent_fd,
                collision_target,
            )
        except OSError as error:
            if (
                atomic_move_error_code(error)
                is not ExecutorErrorCode.DESTINATION_COLLISION
            ):
                return _failure(atomic_move_error_code(error))
        else:
            if (
                _state(
                    source_fd,
                    collision_source,
                    collision_source_identity,
                )
                == "absent"
                and _state(
                    destination_parent_fd,
                    collision_target,
                    collision_source_identity,
                )
                == "expected"
            ):
                owned[:] = [
                    item
                    for item in owned
                    if not (
                        item[0] == source_fd
                        and item[1] == collision_source
                        or item[0] == destination_parent_fd
                        and item[1] == collision_target
                    )
                ]
                owned.append(
                    (
                        destination_parent_fd,
                        collision_target,
                        collision_source_identity,
                    )
                )
                return _failure(
                    ExecutorErrorCode.ATOMIC_MOVE_UNSUPPORTED
                )
            return MoveCapability(MoveCapabilityStatus.UNCERTAIN)
        if (
            _state(
                source_fd,
                collision_source,
                collision_source_identity,
            )
            != "expected"
            or _state(
                destination_parent_fd,
                collision_target,
                collision_target_identity,
            )
            != "expected"
        ):
            return MoveCapability(MoveCapabilityStatus.UNCERTAIN)
        os.fsync(source_fd)
        os.fsync(destination_parent_fd)
        os.fsync(destination_fd)
        return MoveCapability(MoveCapabilityStatus.SUPPORTED)
    except OSError:
        return MoveCapability(MoveCapabilityStatus.UNCERTAIN)
    finally:
        cleanup_failed = False
        for parent_fd, name, identity in reversed(owned):
            try:
                _remove_owned_empty(parent_fd, name, identity)
            except OSError:
                cleanup_failed = True
        if destination_parent_fd is not None:
            os.close(destination_parent_fd)
        os.close(destination_fd)
        os.close(source_fd)
        if cleanup_failed:
            return MoveCapability(
                MoveCapabilityStatus.UNCERTAIN,
                "probe_cleanup_failed",
            )


def _failure(code: ExecutorErrorCode) -> MoveCapability:
    status = {
        ExecutorErrorCode.ATOMIC_MOVE_UNSUPPORTED: (
            MoveCapabilityStatus.UNSUPPORTED
        ),
        ExecutorErrorCode.CROSS_FILESYSTEM: (
            MoveCapabilityStatus.CROSS_FILESYSTEM
        ),
    }.get(code, MoveCapabilityStatus.UNCERTAIN)
    return MoveCapability(status, code.value)


def _open_root(root: AuthorizedRoot) -> int:
    file_descriptor = os.open(
        root.path,
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0),
    )
    metadata = os.fstat(file_descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != root.device
        or metadata.st_ino != root.inode
    ):
        os.close(file_descriptor)
        raise OSError
    return file_descriptor


def _mkdir_owned(parent_fd: int, name: str) -> tuple[int, int]:
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    os.fsync(parent_fd)
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError
    return metadata.st_dev, metadata.st_ino


def _open_owned_directory(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
) -> int:
    opened = os.open(
        name,
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    metadata = os.fstat(opened)
    if (metadata.st_dev, metadata.st_ino) != identity:
        os.close(opened)
        raise OSError
    return opened


def _state(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
) -> str:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "other"
    return (
        "expected"
        if stat.S_ISDIR(metadata.st_mode)
        and (metadata.st_dev, metadata.st_ino) == identity
        else "other"
    )


def _remove_owned_empty(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
) -> None:
    state = _state(parent_fd, name, identity)
    if state == "absent":
        return
    if state != "expected":
        raise OSError
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)
