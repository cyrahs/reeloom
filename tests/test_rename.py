from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from reeloom.rename import RenameFailure, classify, rename_noreplace


def test_move_succeeds_when_the_destination_is_free(tmp_path: Path) -> None:
    source = tmp_path / "a"
    source.write_bytes(b"payload")

    rename_noreplace(source, tmp_path / "b")

    assert (tmp_path / "b").read_bytes() == b"payload"
    assert not source.exists()


def test_existing_destination_is_refused_and_left_intact(tmp_path: Path) -> None:
    source = tmp_path / "a"
    destination = tmp_path / "b"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")

    with pytest.raises(FileExistsError):
        rename_noreplace(source, destination)

    assert destination.read_bytes() == b"old"
    assert source.read_bytes() == b"new"


def test_a_symlink_at_the_destination_also_counts_as_existing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "a"
    source.write_bytes(b"new")
    target = tmp_path / "target"
    target.write_bytes(b"target")
    os.symlink(target, tmp_path / "b")

    with pytest.raises(FileExistsError):
        rename_noreplace(source, tmp_path / "b")

    assert target.read_bytes() == b"target"


def test_missing_source_raises_not_found(tmp_path: Path) -> None:
    with pytest.raises(OSError) as error:
        rename_noreplace(tmp_path / "absent", tmp_path / "b")
    assert error.value.errno == errno.ENOENT


def test_relative_paths_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        rename_noreplace(Path("a"), tmp_path / "b")


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (errno.EEXIST, RenameFailure.COLLISION),
        (errno.ENOENT, RenameFailure.MISSING_SOURCE),
        (errno.EXDEV, RenameFailure.CROSS_FILESYSTEM),
        (errno.EACCES, RenameFailure.PERMISSION_DENIED),
        (errno.ENOSYS, RenameFailure.UNSUPPORTED),
        (errno.EIO, RenameFailure.TRANSIENT),
        (errno.E2BIG, RenameFailure.UNKNOWN),
    ],
)
def test_error_classification(number: int, expected: RenameFailure) -> None:
    assert classify(OSError(number, "x")) is expected


@pytest.fixture
def flag_unsupported(monkeypatch):
    """A filesystem that rejects RENAME_NOREPLACE with EINVAL, as NFS does."""

    import ctypes

    import reeloom.rename as module

    monkeypatch.setattr(module, "_RENAMEAT2", lambda *args: -1)
    monkeypatch.setattr(module, "_RENAMEATX_NP", None)
    monkeypatch.setattr(ctypes, "get_errno", lambda: errno.EINVAL)
    monkeypatch.setattr(module, "_is_fuse", lambda path: False)


def test_unsupported_flag_falls_back_to_link_then_unlink(
    tmp_path: Path, flag_unsupported
) -> None:
    source = tmp_path / "a"
    source.write_bytes(b"payload")

    rename_noreplace(source, tmp_path / "b")

    assert (tmp_path / "b").read_bytes() == b"payload"
    assert not source.exists()


def test_unsupported_flag_still_refuses_an_existing_destination(
    tmp_path: Path, flag_unsupported
) -> None:
    source = tmp_path / "a"
    destination = tmp_path / "b"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")

    with pytest.raises(FileExistsError):
        rename_noreplace(source, destination)

    assert destination.read_bytes() == b"old"
    assert source.read_bytes() == b"new"


def test_unsupported_flag_moves_a_directory_with_a_checked_rename(
    tmp_path: Path, flag_unsupported
) -> None:
    source = tmp_path / "dir"
    source.mkdir()
    (source / "f").write_bytes(b"x")

    rename_noreplace(source, tmp_path / "moved")

    assert (tmp_path / "moved" / "f").read_bytes() == b"x"
    assert not source.exists()
