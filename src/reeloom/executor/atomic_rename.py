from __future__ import annotations

import ctypes
import errno
import os

_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
_RENAMEATX_NP = getattr(_LIBC, "renameatx_np", None)
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
        raise OSError(error_number, os.strerror(error_number))
