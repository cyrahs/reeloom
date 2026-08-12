from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reeloom.trash import (
    TRASH_DIR,
    TrashError,
    list_trash_entries,
    prune_trash,
    purge_run_trash,
    rmdir_if_empty,
    trash_relative,
)


def _drop(root: Path, run_id: str, relative: str, content: bytes = b"x") -> Path:
    path = root / TRASH_DIR / run_id / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_trash_relative_layout() -> None:
    assert (
        trash_relative("run-1", "library", "Show (2024)/S01/Show S01E01.mkv")
        == f"{TRASH_DIR}/run-1/library/Show (2024)/S01/Show S01E01.mkv"
    )


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", ".hidden"])
def test_trash_relative_rejects_bad_components(bad: str) -> None:
    with pytest.raises(TrashError):
        trash_relative(bad, "library", "file.mkv")
    with pytest.raises(TrashError):
        trash_relative("run-1", bad, "file.mkv")


def test_list_trash_entries(tmp_path: Path) -> None:
    _drop(tmp_path, "run-a", "S01/a.mkv", b"aaaa")
    _drop(tmp_path, "run-a", "S01/b.mkv", b"bb")
    _drop(tmp_path, "run-b", "movie.mkv", b"c")

    entries = list_trash_entries(tmp_path)
    assert [(entry.run_id, entry.files, entry.bytes) for entry in entries] == [
        ("run-a", 2, 6),
        ("run-b", 1, 1),
    ]


def test_list_trash_entries_without_trash_dir(tmp_path: Path) -> None:
    assert list_trash_entries(tmp_path) == []


def test_purge_removes_only_the_named_run(tmp_path: Path) -> None:
    _drop(tmp_path, "run-a", "a.mkv", b"aaaa")
    _drop(tmp_path, "run-b", "b.mkv", b"bb")

    assert purge_run_trash(tmp_path, "run-a") == (1, 4)
    assert not (tmp_path / TRASH_DIR / "run-a").exists()
    assert (tmp_path / TRASH_DIR / "run-b" / "b.mkv").exists()


def test_purge_removes_empty_trash_dir(tmp_path: Path) -> None:
    _drop(tmp_path, "run-a", "a.mkv")
    purge_run_trash(tmp_path, "run-a")
    assert not (tmp_path / TRASH_DIR).exists()


def test_purge_missing_entry_is_a_noop(tmp_path: Path) -> None:
    _drop(tmp_path, "run-a", "a.mkv")
    assert purge_run_trash(tmp_path, "run-x") == (0, 0)
    assert purge_run_trash(tmp_path / "elsewhere", "run-a") == (0, 0)


def test_purge_refuses_symlinked_entry(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.mkv").write_bytes(b"data")
    (tmp_path / TRASH_DIR).mkdir()
    (tmp_path / TRASH_DIR / "run-a").symlink_to(victim)

    with pytest.raises(TrashError):
        purge_run_trash(tmp_path, "run-a")
    assert (victim / "keep.mkv").exists()


def test_purge_refuses_run_id_traversal(tmp_path: Path) -> None:
    with pytest.raises(TrashError):
        purge_run_trash(tmp_path, "../victim")


def test_prune_removes_empty_skeletons_but_no_files(tmp_path: Path) -> None:
    kept = _drop(tmp_path, "run-a", "library/S01/a.mkv")
    (tmp_path / TRASH_DIR / "run-b" / "extra-1" / "Show").mkdir(parents=True)

    prune_trash(tmp_path)

    assert kept.exists()
    assert not (tmp_path / TRASH_DIR / "run-b").exists()


def test_prune_removes_an_empty_trash_dir_entirely(tmp_path: Path) -> None:
    (tmp_path / TRASH_DIR / "run-a" / "library").mkdir(parents=True)
    prune_trash(tmp_path)
    assert not (tmp_path / TRASH_DIR).exists()


def test_prune_without_a_trash_dir_is_a_noop(tmp_path: Path) -> None:
    prune_trash(tmp_path)
    assert not (tmp_path / TRASH_DIR).exists()


def test_prune_never_calls_rmdir_on_a_non_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CloudDrive-style FUSE mounts implement rmdir as a recursive delete
    # instead of failing with ENOTEMPTY; pruning must not rely on that
    # failure to protect the trashed files.
    kept = _drop(tmp_path, "run-a", "inbound/Show/a.mkv")

    def cloud_rmdir(self: Path) -> None:
        shutil.rmtree(self)

    monkeypatch.setattr(Path, "rmdir", cloud_rmdir)
    prune_trash(tmp_path)

    assert kept.exists()


def test_rmdir_if_empty_removes_only_empty_directories(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    full = tmp_path / "full"
    full.mkdir()
    (full / "keep.mkv").write_bytes(b"data")

    rmdir_if_empty(empty)
    rmdir_if_empty(full)
    rmdir_if_empty(tmp_path / "absent")

    assert not empty.exists()
    assert (full / "keep.mkv").exists()


def test_rmdir_if_empty_leaves_symlinks_alone(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    link = tmp_path / "link"
    link.symlink_to(victim)

    rmdir_if_empty(link)

    assert link.is_symlink()
    assert victim.is_dir()
