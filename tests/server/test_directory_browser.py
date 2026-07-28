from __future__ import annotations

from pathlib import Path

import pytest

from reeloom.server.directory_browser import PodDirectoryBrowser
from reeloom.server.errors import ServerError, ServerErrorCode


def test_lists_only_real_directories_without_env_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "Anime").mkdir()
    (tmp_path / "Movie").mkdir()
    (tmp_path / "file.mkv").write_bytes(b"video")
    (tmp_path / ".env-private").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "Anime")

    result = PodDirectoryBrowser(tmp_path).list("")

    assert result == {
        "path": "",
        "absolute_path": tmp_path.as_posix(),
        "parent": None,
        "directories": [
            {"name": "Anime", "path": "Anime"},
            {"name": "Movie", "path": "Movie"},
        ],
    }


def test_navigates_relative_paths_and_returns_parent(
    tmp_path: Path,
) -> None:
    season = tmp_path / "媒体" / "动画"
    season.mkdir(parents=True)

    result = PodDirectoryBrowser(tmp_path).list("媒体/动画")

    assert result["absolute_path"] == season.as_posix()
    assert result["parent"] == "媒体"


@pytest.mark.parametrize("path", ("../private", "/private", ".env-secret"))
def test_rejects_escape_and_env_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(ServerError) as raised:
        PodDirectoryBrowser(tmp_path).list(path)

    assert raised.value.code is ServerErrorCode.INVALID_DIRECTORY_PATH


def test_refuses_symlink_traversal(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "linked").symlink_to(target)

    with pytest.raises(ServerError) as raised:
        PodDirectoryBrowser(tmp_path).list("linked")

    assert raised.value.code is ServerErrorCode.DIRECTORY_NOT_FOUND


def test_fails_closed_when_directory_listing_is_too_large(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    monkeypatch.setattr(
        "reeloom.server.directory_browser._MAX_DIRECTORIES",
        1,
    )

    with pytest.raises(ServerError) as raised:
        PodDirectoryBrowser(tmp_path).list("")

    assert raised.value.code is ServerErrorCode.DIRECTORY_TOO_LARGE
