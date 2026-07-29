from __future__ import annotations

import ctypes
import errno
import logging
import os
from enum import StrEnum

_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
_RENAMEATX_NP = getattr(_LIBC, "renameatx_np", None)
_LOGGER = logging.getLogger(__name__)
for function in (_RENAMEAT2, _RENAMEATX_NP):
    if function is not None:
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int


class AtomicRenameFailure(StrEnum):
    COLLISION = "collision"
    CROSS_FILESYSTEM = "cross_filesystem"
    PERMISSION_DENIED = "permission_denied"
    TRANSIENT_IO = "transient_io"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


_UNSUPPORTED = frozenset(
    {
        errno.EINVAL,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }
)
_TRANSIENT = frozenset(
    {
        errno.EAGAIN,
        errno.EBUSY,
        errno.EINTR,
        errno.EIO,
        getattr(errno, "ESTALE", errno.EIO),
        errno.ETIMEDOUT,
    }
)


def classify_atomic_rename_error(
    error: OSError,
) -> AtomicRenameFailure:
    error_number = error.errno
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        return AtomicRenameFailure.COLLISION
    if error_number == errno.EXDEV:
        return AtomicRenameFailure.CROSS_FILESYSTEM
    if error_number in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return AtomicRenameFailure.PERMISSION_DENIED
    if error_number in _UNSUPPORTED:
        return AtomicRenameFailure.UNSUPPORTED
    if error_number in _TRANSIENT:
        return AtomicRenameFailure.TRANSIENT_IO
    return AtomicRenameFailure.UNKNOWN


def rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    """Atomically rename without replacing an existing destination."""

    if _RENAMEAT2 is not None:
        function = _RENAMEAT2
        flag = _RENAME_NOREPLACE
    elif _RENAMEATX_NP is not None:
        function = _RENAMEATX_NP
        flag = _RENAME_EXCL
    else:
        raise OSError(errno.ENOSYS, "exclusive rename unavailable")
    result = function(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        flag,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        _LOGGER.warning(
            "atomic no-replace rename failed category=%s errno=%s",
            classify_atomic_rename_error(
                OSError(error_number, os.strerror(error_number))
            ).value,
            errno.errorcode.get(error_number, "UNKNOWN"),
        )
        raise OSError(error_number, os.strerror(error_number))
