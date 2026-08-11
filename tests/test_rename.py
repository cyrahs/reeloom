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
