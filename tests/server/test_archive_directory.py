from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from reeloom.kernel.archive_directory import (
    ArchiveDirectoryCapability,
    ArchiveDirectoryListing,
    ArchiveSearchRecord,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.archive_directory import ArchiveDirectoryError
from reeloom.server import archive_directory
from reeloom.server.archive_directory import (
    FilesystemArchiveDirectoryBrowser,
)
from reeloom.server.archive_report import archive_report_from_state
from reeloom.runtime.events import RunStarted
from reeloom.runtime.reducer import reduce_event


def _browser(root: Path) -> FilesystemArchiveDirectoryBrowser:
    return FilesystemArchiveDirectoryBrowser(
        run_id="run-browser",
        root=AuthorizedRoot.create(root),
    )


def test_search_is_cached_and_list_descends_exactly_one_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "library"
    season = root / "旧项目" / "S01"
    season.mkdir(parents=True)
    (season / "Old S01E01.mkv").write_bytes(b"video")
    (season / "notes.txt").write_text("ignore")
    (root / ".hidden").mkdir()
    (root / ".env-project").mkdir()
    (root / "link").symlink_to(root / "旧项目", target_is_directory=True)
    browser = _browser(root)
    scans = 0
    original = browser._scan_root

    def counted_scan():
        nonlocal scans
        scans += 1
        return original()

    monkeypatch.setattr(browser, "_scan_root", counted_scan)

    first = asyncio.run(
        browser.search(
            work_type=TmdbWorkType.ANIME,
            tmdb_id=42,
            mode="name",
            name="旧项目",
            cursor=0,
            limit=50,
        )
    )
    second = asyncio.run(
        browser.search(
            work_type=TmdbWorkType.ANIME,
            tmdb_id=42,
            mode="selected_tmdb_id",
            name=None,
            cursor=0,
            limit=50,
        )
    )

    assert scans == 1
    assert [item.name for item in first[0]] == ["旧项目"]
    assert second[0] == ()
    project_id = first[0][0].directory_id
    children, videos, _, complete = asyncio.run(
        browser.list(directory_id=project_id, cursor=0, limit=100)
    )
    assert [item.name for item in children] == ["S01"]
    assert videos == ()
    assert complete
    season_children, season_videos, _, _ = asyncio.run(
        browser.list(
            directory_id=children[0].directory_id,
            cursor=0,
            limit=100,
        )
    )
    assert season_children == ()
    assert season_videos == ("Old S01E01.mkv",)


def test_forged_or_replaced_directory_capability_fails_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    project = root / "Project {tmdb-42}"
    project.mkdir(parents=True)
    browser = _browser(root)
    matches, _, _, _ = asyncio.run(
        browser.search(
            work_type=TmdbWorkType.TV_SERIES,
            tmdb_id=42,
            mode="selected_tmdb_id",
            name=None,
            cursor=0,
            limit=50,
        )
    )

    with pytest.raises(ArchiveDirectoryError) as forged:
        asyncio.run(
            browser.list(
                directory_id="dir-forged",
                cursor=0,
                limit=100,
            )
        )
    assert forged.value.code == "unknown_directory_id"

    project.rename(root / "moved")
    project.mkdir()
    with pytest.raises(ArchiveDirectoryError) as stale:
        asyncio.run(
            browser.list(
                directory_id=matches[0].directory_id,
                cursor=0,
                limit=100,
            )
        )
    assert stale.value.code == "directory_capability_stale"


def test_cached_listing_revalidates_directory_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    project = root / "Project {tmdb-42}"
    project.mkdir(parents=True)
    browser = _browser(root)
    matches, _, _, _ = asyncio.run(
        browser.search(
            work_type=TmdbWorkType.TV_SERIES,
            tmdb_id=42,
            mode="selected_tmdb_id",
            name=None,
            cursor=0,
            limit=50,
        )
    )
    asyncio.run(
        browser.list(
            directory_id=matches[0].directory_id,
            cursor=0,
            limit=100,
        )
    )
    project.rename(root / "moved")
    project.mkdir()

    with pytest.raises(ArchiveDirectoryError) as stale:
        asyncio.run(
            browser.list(
                directory_id=matches[0].directory_id,
                cursor=0,
                limit=100,
            )
        )

    assert stale.value.code == "directory_capability_stale"


def test_tmdb_search_uses_an_exact_numeric_token(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    (root / "Exact tmdb-42 legacy").mkdir(parents=True)
    (root / "Prefix tmdb-420 legacy").mkdir()
    (root / "Embeddednotmdb-42 legacy").mkdir()

    matches, _, complete, _ = asyncio.run(
        _browser(root).search(
            work_type=TmdbWorkType.MOVIE,
            tmdb_id=42,
            mode="selected_tmdb_id",
            name=None,
            cursor=0,
            limit=50,
        )
    )

    assert [item.name for item in matches] == [
        "Exact tmdb-42 legacy"
    ]
    assert complete


def test_search_skips_names_that_cannot_receive_capabilities(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    (root / "Bad\\Name tmdb-42").mkdir(parents=True)

    matches, _, complete, _ = asyncio.run(
        _browser(root).search(
            work_type=TmdbWorkType.MOVIE,
            tmdb_id=42,
            mode="selected_tmdb_id",
            name=None,
            cursor=0,
            limit=50,
        )
    )

    assert matches == ()
    assert complete


def test_listing_stops_at_depth_three_and_reports_incomplete(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    deepest = root / "Project tmdb-42" / "S01" / "Disc" / "Nested"
    deepest.mkdir(parents=True)
    browser = _browser(root)
    matches, _, _, _ = asyncio.run(
        browser.search(
            work_type=TmdbWorkType.ANIME,
            tmdb_id=42,
            mode="selected_tmdb_id",
            name=None,
            cursor=0,
            limit=50,
        )
    )
    level_two, _, _, _ = asyncio.run(
        browser.list(
            directory_id=matches[0].directory_id,
            cursor=0,
            limit=100,
        )
    )
    level_three, _, _, _ = asyncio.run(
        browser.list(
            directory_id=level_two[0].directory_id,
            cursor=0,
            limit=100,
        )
    )

    children, videos, next_cursor, complete = asyncio.run(
        browser.list(
            directory_id=level_three[0].directory_id,
            cursor=0,
            limit=100,
        )
    )

    assert children == ()
    assert videos == ()
    assert next_cursor is None
    assert not complete


def test_restored_capability_is_bound_to_its_run(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    (root / "Project tmdb-42").mkdir(parents=True)
    first = _browser(root)
    matches, _, _, _ = asyncio.run(
        first.search(
            work_type=TmdbWorkType.ANIME,
            tmdb_id=42,
            mode="selected_tmdb_id",
            name=None,
            cursor=0,
            limit=50,
        )
    )
    second = FilesystemArchiveDirectoryBrowser(
        run_id="run-other",
        root=AuthorizedRoot.create(root),
    )

    with pytest.raises(ArchiveDirectoryError) as wrong_run:
        second.restore(matches)

    assert wrong_run.value.code == "directory_capability_wrong_run"


def test_single_io_lane_times_out_then_opens_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_directory, "_IO_TIMEOUT_SECONDS", 0.01)
    lane = archive_directory._DirectoryIOLane()
    release = threading.Event()

    async def exercise() -> None:
        with pytest.raises(ArchiveDirectoryError) as timeout:
            await lane.run(lambda: release.wait(1))
        assert timeout.value.code == "directory_io_timeout"
        with pytest.raises(ArchiveDirectoryError) as busy:
            await lane.run(lambda: None)
        assert busy.value.code == "directory_io_busy"

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_archive_report_requires_contiguous_pages_and_is_tree_ordered() -> None:
    observed_at = datetime(2026, 7, 28, tzinfo=UTC)
    root_a = ArchiveDirectoryCapability(
        "run-browser",
        "dir-a",
        None,
        PurePosixPath("Project A"),
        "Project A",
        1,
        1,
        1,
        1,
        1,
    )
    season = ArchiveDirectoryCapability(
        "run-browser",
        "dir-season",
        "dir-a",
        PurePosixPath("Project A/S01"),
        "S01",
        2,
        1,
        2,
        1,
        1,
    )
    root_b = ArchiveDirectoryCapability(
        "run-browser",
        "dir-b",
        None,
        PurePosixPath("Project B"),
        "Project B",
        1,
        1,
        3,
        1,
        1,
    )
    searches = (
        ArchiveSearchRecord(
            "search-1",
            "selected_tmdb_id",
            "tmdb-42",
            42,
            TmdbWorkType.ANIME,
            ("dir-a",),
            0,
            1,
            False,
            observed_at,
        ),
        ArchiveSearchRecord(
            "search-2",
            "selected_tmdb_id",
            "tmdb-42",
            42,
            TmdbWorkType.ANIME,
            ("dir-b",),
            1,
            None,
            True,
            observed_at,
        ),
    )
    listings = (
        ArchiveDirectoryListing(
            "list-a-1",
            "dir-a",
            ("dir-season",),
            ("Root S01E01.mkv",),
            ((1, 1),),
            0,
            1,
            False,
            observed_at,
        ),
        ArchiveDirectoryListing(
            "list-a-2",
            "dir-a",
            (),
            ("Root S01E02.mkv",),
            ((1, 2),),
            1,
            None,
            True,
            observed_at,
        ),
        ArchiveDirectoryListing(
            "list-season",
            "dir-season",
            (),
            ("Season S01E03.mkv",),
            ((1, 3),),
            0,
            None,
            True,
            observed_at,
        ),
        ArchiveDirectoryListing(
            "list-b",
            "dir-b",
            (),
            ("Other S01E01.mkv",),
            ((1, 1),),
            0,
            None,
            True,
            observed_at,
        ),
    )
    state = replace(
        reduce_event(
            None,
            RunStarted("run-browser", TmdbWorkType.ANIME),
        ),
        archive_directory_capabilities=(root_a, season, root_b),
        archive_searches=searches,
        archive_directory_listings=listings,
    )

    report = archive_report_from_state(state)

    assert report is not None
    assert report["status"] == "checked"
    entries = report["entries"]
    assert [entry["name"] for entry in entries] == [
        "Project A",
        "Root S01E01.mkv",
        "Root S01E02.mkv",
        "S01",
        "Season S01E03.mkv",
        "Project B",
        "Other S01E01.mkv",
    ]
    assert entries[4]["parent_entry_id"] == entries[3]["entry_id"]
    skipped = archive_report_from_state(
        replace(state, archive_searches=(searches[1],))
    )
    assert skipped is not None
    assert skipped["status"] == "incomplete"
