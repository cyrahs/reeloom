from __future__ import annotations

import fcntl
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from reeloom.server.errors import ServerError, ServerErrorCode


@dataclass(slots=True)
class ProcessLock:
    """A no-follow lifetime lock for one state root."""

    _descriptor: int
    _closed: bool = False

    @classmethod
    def acquire(cls, state_root: Path) -> ProcessLock:
        root_descriptor = -1
        lock_descriptor = -1
        try:
            root_descriptor = os.open(
                state_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            root_stat = os.fstat(root_descriptor)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or root_stat.st_mode & 0o022
            ):
                raise OSError
            lock_descriptor = os.open(
                "server.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_descriptor,
            )
            lock_stat = os.fstat(lock_descriptor)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_nlink != 1
            ):
                raise OSError
            fcntl.flock(
                lock_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            return cls(lock_descriptor)
        except BlockingIOError:
            if lock_descriptor >= 0:
                os.close(lock_descriptor)
            raise ServerError(
                ServerErrorCode.INSTANCE_ALREADY_RUNNING
            ) from None
        except OSError:
            if lock_descriptor >= 0:
                os.close(lock_descriptor)
            raise ServerError(ServerErrorCode.UNSAFE_STATE_ROOT) from None
        finally:
            if root_descriptor >= 0:
                os.close(root_descriptor)

    def close(self) -> None:
        if self._closed:
            return
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._descriptor)
            self._closed = True

    def __enter__(self) -> ProcessLock:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
