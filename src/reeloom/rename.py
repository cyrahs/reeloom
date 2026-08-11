"""No-replace rename.

The single primitive the whole safety model rests on: a rename that fails
rather than overwriting. ``renameat2(RENAME_NOREPLACE)`` on Linux,
``renameatx_np(RENAME_EXCL)`` on Darwin. FUSE mounts that answer "not
supported" fall back to check-then-rename, which is what CloudDrive-style
mounts need; that path is logged because it is not atomic.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import sys
from enum import StrEnum
from pathlib import Path

_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_FUSE_SUPER_MAGIC = 0x65735546

_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
_RENAMEATX_NP = getattr(_LIBC, "renameatx_np", None)
_STATFS = getattr(_LIBC, "statfs", None)

for _function in (_RENAMEAT2, _RENAMEATX_NP):
    if _function is not None:
        _function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        _function.restype = ctypes.c_int

_LOGGER = logging.getLogger(__name__)


class RenameFailure(StrEnum):
    COLLISION = "collision"
    MISSING_SOURCE = "missing_source"
    CROSS_FILESYSTEM = "cross_filesystem"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED = "unsupported"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


_UNSUPPORTED_ERRORS = frozenset(
    {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}
)
_TRANSIENT_ERRORS = frozenset(
    {errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.EIO, errno.ESTALE, errno.ETIMEDOUT}
)


def classify(error: OSError) -> RenameFailure:
    number = error.errno
    if number in {errno.EEXIST, errno.ENOTEMPTY}:
        return RenameFailure.COLLISION
    if number == errno.ENOENT:
        return RenameFailure.MISSING_SOURCE
    if number == errno.EXDEV:
        return RenameFailure.CROSS_FILESYSTEM
    if number in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return RenameFailure.PERMISSION_DENIED
    if number in _UNSUPPORTED_ERRORS:
        return RenameFailure.UNSUPPORTED
    if number in _TRANSIENT_ERRORS:
        return RenameFailure.TRANSIENT
    return RenameFailure.UNKNOWN


# Differs by platform, and only ignorable because every path here is
# absolute; kept correct so a relative path would still behave.
_AT_FDCWD = -2 if sys.platform == "darwin" else -100


def rename_noreplace(source: Path, destination: Path) -> None:
    """Rename, raising FileExistsError rather than replacing the destination."""

    if not source.is_absolute() or not destination.is_absolute():
        raise ValueError("rename_noreplace requires absolute paths")

    if _RENAMEAT2 is not None:
        function, flag = _RENAMEAT2, _RENAME_NOREPLACE
    elif _RENAMEATX_NP is not None:
        function, flag = _RENAMEATX_NP, _RENAME_EXCL
    else:
        _checked_rename(source, destination, reason="no exclusive rename syscall")
        return

    result = function(
        _AT_FDCWD,
        os.fsencode(str(source)),
        _AT_FDCWD,
        os.fsencode(str(destination)),
        flag,
    )
    if result == 0:
        return

    number = ctypes.get_errno()
    error = OSError(number, os.strerror(number), str(source))
    failure = classify(error)
    if failure is RenameFailure.UNSUPPORTED and _is_fuse(source):
        _checked_rename(source, destination, reason="FUSE mount")
        return
    if failure is RenameFailure.COLLISION:
        raise FileExistsError(number, os.strerror(number), str(destination))
    raise error


def _checked_rename(source: Path, destination: Path, *, reason: str) -> None:
    """Degraded fallback: verify absence, then rename.

    Not atomic — a destination created in the gap would be overwritten. Only
    reached on backends that cannot do the exclusive rename at all.
    """

    if destination.exists() or destination.is_symlink():
        raise FileExistsError(
            errno.EEXIST, os.strerror(errno.EEXIST), str(destination)
        )
    _LOGGER.warning("degraded non-atomic rename (%s): %s", reason, destination)
    os.rename(source, destination)


def _is_fuse(path: Path) -> bool:
    if not sys.platform.startswith("linux") or _STATFS is None:
        return False
    buffer = ctypes.create_string_buffer(256)
    if _STATFS(os.fsencode(str(path.parent)), ctypes.byref(buffer)) != 0:
        return False
    return ctypes.c_long.from_buffer(buffer).value == _FUSE_SUPER_MAGIC
