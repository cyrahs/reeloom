from __future__ import annotations

from pathlib import Path

import os

from reeloom.library import find_existing_folder
from reeloom.models import MediaIdentity, MediaType

IDENTITY = MediaIdentity(MediaType.ANIME, 123, "Show", 2024)


def test_no_match_returns_none(roots: tuple[Path, Path]) -> None:
    _, library = roots
    (library / "Other (2020) {tmdb-999}").mkdir()
    assert find_existing_folder(library, IDENTITY) is None


def test_tagged_folder_is_found_by_id_even_with_a_different_title(
    roots: tuple[Path, Path],
) -> None:
    _, library = roots
    (library / "Different Name (2024) {tmdb-123}").mkdir()
    found = find_existing_folder(library, IDENTITY)
    assert found is not None and found.name == "Different Name (2024) {tmdb-123}"


def test_untagged_folder_is_matched_by_title(roots: tuple[Path, Path]) -> None:
    _, library = roots
    (library / "Show").mkdir()
    found = find_existing_folder(library, IDENTITY)
    assert found is not None and found.needs_tmdb_id


def test_untagged_folder_with_a_year_is_matched(roots: tuple[Path, Path]) -> None:
    _, library = roots
    (library / "Show (2024)").mkdir()
    found = find_existing_folder(library, IDENTITY)
    assert found is not None and found.name == "Show (2024)"


def test_similar_but_different_title_is_not_matched(
    roots: tuple[Path, Path],
) -> None:
    _, library = roots
    (library / "Show Second Season").mkdir()
    assert find_existing_folder(library, IDENTITY) is None


def test_tagged_folder_wins_and_the_old_one_is_left_alone(
    roots: tuple[Path, Path],
) -> None:
    _, library = roots
    (library / "Show").mkdir()
    (library / "Show (2024) {tmdb-123}").mkdir()

    found = find_existing_folder(library, IDENTITY)
    assert found is not None and found.name == "Show (2024) {tmdb-123}"
    assert not found.needs_tmdb_id


def test_symlinked_folder_is_ignored(roots: tuple[Path, Path]) -> None:
    inbound, library = roots
    (inbound / "elsewhere").mkdir()
    os.symlink(inbound / "elsewhere", library / "Show")
    assert find_existing_folder(library, IDENTITY) is None


def test_missing_library_root_is_not_an_error(tmp_path: Path) -> None:
    assert find_existing_folder(tmp_path / "absent", IDENTITY) is None
