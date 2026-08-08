from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from reeloom.executor.folder_housekeeping_v2 import (
    FolderHousekeepingExecutor,
    FolderHousekeepingOutcome,
    housekeeping_target_name,
)


def _names(run_id: str = "run:1") -> tuple[str, str]:
    return "Incoming", housekeeping_target_name("Incoming", run_id)


def test_forward_housekeeping_archives_without_overwrite(tmp_path: Path) -> None:
    source, target = _names()
    (tmp_path / source).mkdir()
    (tmp_path / source / "note.txt").write_text("kept", encoding="utf-8")

    result = FolderHousekeepingExecutor(observation_delays=(0.0,)).execute(
        root=tmp_path,
        source_folder=source,
        target_folder=target,
        action="archive",
    )

    assert result.outcome is FolderHousekeepingOutcome.COMPLETED
    assert not (tmp_path / source).exists()
    assert (tmp_path / "archive" / target / "note.txt").read_text() == "kept"


def test_housekeeping_collision_is_warning_only(tmp_path: Path) -> None:
    source, target = _names()
    (tmp_path / source).mkdir()
    (tmp_path / "archive" / target).mkdir(parents=True)

    result = FolderHousekeepingExecutor(observation_delays=(0.0,)).execute(
        root=tmp_path,
        source_folder=source,
        target_folder=target,
        action="archive",
    )

    assert result.outcome is FolderHousekeepingOutcome.COLLISION
    assert result.warning == "destination_collision"
    assert (tmp_path / source).is_dir()


def test_housekeeping_rejects_symlink_source(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, tmp_path / "Incoming")
    _, target = _names()

    result = FolderHousekeepingExecutor(observation_delays=(0.0,)).execute(
        root=tmp_path,
        source_folder="Incoming",
        target_folder=target,
        action="fail",
    )

    assert result.outcome is FolderHousekeepingOutcome.UNSAFE
    assert (tmp_path / "Incoming").is_symlink()


def test_housekeeping_accepts_reported_failure_after_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = _names()
    (tmp_path / source).mkdir()

    def moved_then_failed(
        source_fd: int,
        source_name: str,
        target_fd: int,
        target_name: str,
    ) -> None:
        os.rename(
            source_name,
            target_name,
            src_dir_fd=source_fd,
            dst_dir_fd=target_fd,
        )
        raise OSError(errno.EIO, "remote status lost")

    monkeypatch.setattr(
        "reeloom.executor.folder_housekeeping_v2.rename_noreplace",
        moved_then_failed,
    )
    result = FolderHousekeepingExecutor(observation_delays=(0.0,)).execute(
        root=tmp_path,
        source_folder=source,
        target_folder=target,
        action="archive",
    )

    assert result.outcome is FolderHousekeepingOutcome.COMPLETED


def test_housekeeping_target_is_stable_and_bounded() -> None:
    first = housekeeping_target_name("剧" * 80, "run:stable")
    second = housekeeping_target_name("剧" * 80, "run:stable")

    assert first == second
    assert len(first.encode()) <= 255
