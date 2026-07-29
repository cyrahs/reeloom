from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

import reeloom.executor.atomic_rename as rename_module
from reeloom.executor.atomic_rename import (
    AtomicRenameFailure,
    RenameBackend,
    classify_atomic_rename_error,
    rename_noreplace_compatible,
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


def test_fuse_falls_back_to_checked_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "item").write_bytes(b"content")

    def unsupported(*args: object) -> None:
        del args
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(rename_module, "rename_noreplace", unsupported)
    monkeypatch.setattr(rename_module, "_is_fuse_fd", lambda _: True)
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    destination_fd = os.open(
        destination, os.O_RDONLY | os.O_DIRECTORY
    )
    try:
        backend = rename_noreplace_compatible(
            source_fd, "item", destination_fd, "item"
        )
    finally:
        os.close(destination_fd)
        os.close(source_fd)

    assert backend is RenameBackend.FUSE_CHECKED_RENAME
    assert not (source / "item").exists()
    assert (destination / "item").read_bytes() == b"content"


def test_fuse_fallback_preserves_existing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "item").write_bytes(b"source")
    (destination / "item").write_bytes(b"destination")

    def unsupported(*args: object) -> None:
        del args
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(rename_module, "rename_noreplace", unsupported)
    monkeypatch.setattr(rename_module, "_is_fuse_fd", lambda _: True)
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    destination_fd = os.open(
        destination, os.O_RDONLY | os.O_DIRECTORY
    )
    try:
        with pytest.raises(FileExistsError):
            rename_noreplace_compatible(
                source_fd, "item", destination_fd, "item"
            )
    finally:
        os.close(destination_fd)
        os.close(source_fd)

    assert (source / "item").read_bytes() == b"source"
    assert (destination / "item").read_bytes() == b"destination"


def test_non_fuse_keeps_unsupported_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "item").write_bytes(b"content")

    def unsupported(*args: object) -> None:
        del args
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(rename_module, "rename_noreplace", unsupported)
    monkeypatch.setattr(rename_module, "_is_fuse_fd", lambda _: False)
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    destination_fd = os.open(
        destination, os.O_RDONLY | os.O_DIRECTORY
    )
    try:
        with pytest.raises(OSError) as raised:
            rename_noreplace_compatible(
                source_fd, "item", destination_fd, "item"
            )
    finally:
        os.close(destination_fd)
        os.close(source_fd)

    assert raised.value.errno == errno.EOPNOTSUPP
    assert (source / "item").is_file()
    assert not (destination / "item").exists()
