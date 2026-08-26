from __future__ import annotations

import os
from pathlib import Path

import pytest

from reeloom.models import FileKind, ReeloomError
from reeloom.scanner import (
    FolderShape,
    StabilityTracker,
    classify,
    discover_folders,
    folder_shape,
    safe_relative,
    snapshot_folder,
)
from tests.conftest import make_files


def test_classify_uses_final_suffix_case_insensitively() -> None:
    assert classify("Show S01E01.MKV") is FileKind.VIDEO
    assert classify("Show S01E01.chs.SRT") is FileKind.SUBTITLE
    assert classify("readme.txt") is FileKind.OTHER
    assert classify("archive.mkv.7z") is FileKind.OTHER


@pytest.mark.parametrize(
    "value",
    ["/absolute", "../escape", "a/../../b", "back\\slash", ".env", "dir/.env.local"],
)
def test_safe_relative_rejects_dangerous_paths(value: str) -> None:
    with pytest.raises(ReeloomError):
        safe_relative(value)


def test_safe_relative_accepts_nested_path() -> None:
    assert safe_relative("Show/S01/ep.mkv").parts == ("Show", "S01", "ep.mkv")


def test_discover_skips_buckets_hidden_loose_files_and_symlinks(
    roots: tuple[Path, Path],
) -> None:
    inbound, _ = roots
    for name in ("Show A", "archive", "fail", "in_progress", ".hidden"):
        (inbound / name).mkdir()
    (inbound / "loose.mkv").write_bytes(b"x")
    os.symlink(inbound / "Show A", inbound / "linked")

    assert discover_folders(inbound) == ["Show A"]


def test_snapshot_assigns_stable_per_kind_ordinals(roots: tuple[Path, Path]) -> None:
    inbound, _ = roots
    folder = inbound / "Show"
    make_files(
        folder,
        "ep02.mkv",
        "ep01.mkv",
        "subs/ep01.chs.srt",
        "notes.txt",
    )

    snapshot = snapshot_folder(folder)
    by_path = {item.relative_path: item.candidate_id for item in snapshot}

    assert by_path["ep01.mkv"] == "V1"
    assert by_path["ep02.mkv"] == "V2"
    assert by_path["subs/ep01.chs.srt"] == "S1"
    assert by_path["notes.txt"] == "O1"
    assert snapshot_folder(folder) == snapshot


def test_snapshot_never_follows_symlinks(roots: tuple[Path, Path]) -> None:
    inbound, library = roots
    folder = inbound / "Show"
    folder.mkdir()
    make_files(library, "secret.mkv")
    os.symlink(library / "secret.mkv", folder / "linked.mkv")
    make_files(folder, "real.mkv")

    assert [item.relative_path for item in snapshot_folder(folder)] == ["real.mkv"]


def test_snapshot_refuses_folder_containing_env_file(
    roots: tuple[Path, Path],
) -> None:
    inbound, _ = roots
    folder = inbound / "Show"
    make_files(folder, "ep01.mkv", ".env")

    with pytest.raises(ReeloomError) as error:
        snapshot_folder(folder)
    assert error.value.code == "env_file_present"


def test_folder_shape_tracks_growth(roots: tuple[Path, Path]) -> None:
    inbound, _ = roots
    folder = inbound / "Show"
    make_files(folder, "ep01.mkv")
    first = folder_shape(folder)
    make_files(folder, "ep02.mkv")

    assert folder_shape(folder) != first
    assert folder_shape(folder).file_count == 2


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_stability_requires_unchanged_shape_for_the_full_window() -> None:
    clock = FakeClock()
    tracker = StabilityTracker(clock=clock)
    key = ("config", "Show")
    growing = FolderShape(1, 100, 1)
    settled = FolderShape(2, 200, 2)

    assert not tracker.is_stable(key, growing, 120)
    clock.now = 119
    assert not tracker.is_stable(key, growing, 120)

    # A late-arriving file resets the window.
    clock.now = 121
    assert not tracker.is_stable(key, settled, 120)
    clock.now = 240
    assert not tracker.is_stable(key, settled, 120)
    clock.now = 242
    assert tracker.is_stable(key, settled, 120)


def test_empty_folder_is_never_stable() -> None:
    tracker = StabilityTracker(clock=FakeClock())
    assert not tracker.is_stable(("c", "f"), FolderShape(0, 0, 0), 0)
