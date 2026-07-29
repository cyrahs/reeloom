from __future__ import annotations

import errno

import pytest

from reeloom.executor.atomic_rename import (
    AtomicRenameFailure,
    classify_atomic_rename_error,
)


@pytest.mark.parametrize(
    "error_number",
    (
        errno.EINVAL,
        errno.ENOSYS,
        errno.EOPNOTSUPP,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    ),
)
def test_unsupported_atomic_rename_errors_are_bounded(
    error_number: int,
) -> None:
    assert classify_atomic_rename_error(
        OSError(error_number, "untrusted backend text")
    ) is AtomicRenameFailure.UNSUPPORTED


@pytest.mark.parametrize(
    ("error_number", "expected"),
    (
        (errno.EEXIST, AtomicRenameFailure.COLLISION),
        (errno.ENOTEMPTY, AtomicRenameFailure.COLLISION),
        (errno.EXDEV, AtomicRenameFailure.CROSS_FILESYSTEM),
        (errno.EIO, AtomicRenameFailure.TRANSIENT_IO),
        (errno.EACCES, AtomicRenameFailure.PERMISSION_DENIED),
        (errno.EPERM, AtomicRenameFailure.PERMISSION_DENIED),
        (errno.EROFS, AtomicRenameFailure.PERMISSION_DENIED),
    ),
)
def test_atomic_rename_error_classes(
    error_number: int,
    expected: AtomicRenameFailure,
) -> None:
    assert classify_atomic_rename_error(
        OSError(error_number, "untrusted backend text")
    ) is expected
