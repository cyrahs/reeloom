from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from reeloom.adapters.archive import extract_subtitles, find_sevenzip, is_archive
from reeloom.models import (
    EpisodeSpan,
    MediaIdentity,
    MediaType,
    Move,
    MoveKind,
    Plan,
    Root,
    Run,
    RunResult,
    RunState,
    SubtitleVariant,
    WatchConfig,
)
from reeloom.server.subtitles import (
    SubtitleAcquisition,
    _episodes_missing_subtitles,
    parse_episode_number,
)
from tests.conftest import make_files
from tests.fakes import FakeDatabase, ScriptedModel, StubClients, call

IDENTITY = MediaIdentity(MediaType.ANIME, 123, "Show", 2024)
LIBRARY_FOLDER = "Show (2024) {tmdb-123}"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("[Group] Show - 08 [1080p][CHS].ass", 8),
        ("Show S01E08.chs.srt", 8),
        ("[Group] Show 第08话.ass", 8),
        ("Show[08].srt", 8),
        ("Show - 08v2 (1080p).ass", 8),
        ("[Group] Show 2024 [12][BDRip].srt", 12),
        ("［Group］ Show － ０８.ass", 8),
        ("[Group] Show 第０８話.ass", 8),
        ("Show OP.ass", None),
        ("readme.txt", None),
    ],
)
def test_episode_number_parsing(filename: str, expected: int | None) -> None:
    assert parse_episode_number(filename) == expected


def test_resolution_is_never_read_as_an_episode_number() -> None:
    assert parse_episode_number("[Group] Show [1080p].ass") is None


def video_move(episode: int) -> Move:
    return Move(
        kind=MoveKind.MEDIA,
        source_root=Root.INBOUND,
        source_path=f"Drop/ep{episode}.mkv",
        dest_root=Root.LIBRARY,
        dest_path=f"{LIBRARY_FOLDER}/S01/Show S01E{episode:02d}.mkv",
        candidate_id=f"V{episode}",
    )


def test_only_episodes_actually_lacking_a_subtitle_are_wanted(
    roots: tuple[Path, Path],
) -> None:
    _, library = roots
    season = library / LIBRARY_FOLDER / "S01"
    make_files(season, "Show S01E01.mkv", "Show S01E02.mkv", "Show S01E01.chs.ass")
    plan = Plan(identity=IDENTITY, moves=(video_move(1), video_move(2)))

    wanted = _episodes_missing_subtitles(plan, library)

    assert set(wanted) == {2}
    assert wanted[2] == EpisodeSpan(1, 2, 2)


def test_a_video_that_never_landed_is_not_wanted(
    roots: tuple[Path, Path],
) -> None:
    _, library = roots
    plan = Plan(identity=IDENTITY, moves=(video_move(1),))
    assert _episodes_missing_subtitles(plan, library) == {}


# ---- archive extraction --------------------------------------------------


@pytest.mark.skipif(find_sevenzip() is None, reason="7-Zip is not installed")
async def test_extraction_keeps_only_subtitles_and_flattens_them(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    make_files(staging, "nested/Show - 01.chs.ass", "notes.txt", "Show - 02.chs.srt")
    archive = tmp_path / "release.7z"
    subprocess.run(
        [find_sevenzip(), "a", "-y", str(archive), "."],
        cwd=staging,
        check=True,
        capture_output=True,
    )

    extracted = await extract_subtitles(archive, tmp_path / "out")

    assert sorted(path.name for path in extracted) == [
        "Show - 01.chs.ass",
        "Show - 02.chs.srt",
    ]
    assert all(path.parent == tmp_path / "out" for path in extracted)


def test_archive_detection() -> None:
    assert is_archive("release.7z")
    assert is_archive("release.RAR")
    assert not is_archive("subtitle.ass")


# ---- end to end ----------------------------------------------------------


class FakeAcgrip:
    """Stands in for the forum client; never touches the network."""

    def __init__(self, archives: Path | dict[int, Path], excerpt: str = "") -> None:
        self.archives = {99: archives} if isinstance(archives, Path) else archives
        self.excerpt = excerpt
        self.searches: list[str] = []
        self.downloaded: list[int] = []

    async def search(self, keyword: str):
        from reeloom.adapters.acgrip import Thread

        self.searches.append(keyword)
        return [Thread(thread_id=1, title="[Group] Show 简体 合集")]

    async def get_thread(self, thread_id: int):
        from reeloom.adapters.acgrip import Attachment, ThreadPage

        return ThreadPage(
            attachments=tuple(
                Attachment(
                    attachment_id=aid,
                    filename=f"Show.S01.{aid}.7z",
                    download_path=f"/forum.php?mod=attachment&aid={aid}",
                )
                for aid in self.archives
            ),
            excerpt=self.excerpt,
        )

    async def download(self, attachment, destination: Path) -> int:
        self.downloaded.append(attachment.attachment_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.archives[attachment.attachment_id].read_bytes())
        return destination.stat().st_size

    async def aclose(self) -> None:
        return None


def make_archive(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    staging = tmp_path / f"staging-{name}"
    staging.mkdir()
    for filename, text in files.items():
        (staging / filename).write_text(text, "utf-8")
    archive = tmp_path / f"{name}.7z"
    subprocess.run(
        [find_sevenzip(), "a", "-y", str(archive), "."],
        cwd=staging,
        check=True,
        capture_output=True,
    )
    return archive


def make_run(database: FakeDatabase, config: WatchConfig, *episodes: int) -> Run:
    run = Run(
        id="run-1",
        config_id=config.id,
        folder_name="Drop",
        state=RunState.ACQUIRING_SUBS,
        plan=Plan(
            identity=IDENTITY,
            moves=tuple(video_move(episode) for episode in episodes),
        ),
    )
    database.runs[run.id] = run
    return run


SIMPLIFIED_TEXT = "这个国家发展了"
TRADITIONAL_TEXT = "這個國家發展了"


@pytest.mark.skipif(find_sevenzip() is None, reason="7-Zip is not installed")
async def test_acquired_subtitles_are_published_next_to_their_episodes(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    inbound, library = roots
    season = library / LIBRARY_FOLDER / "S01"
    make_files(season, "Show S01E01.mkv", "Show S01E02.mkv")

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "[Group] Show - 01 [CHS].ass").write_text("这个国家发展了", "utf-8")
    (staging / "[Group] Show - 02 [CHS].ass").write_text("这个国家发展了", "utf-8")
    archive = tmp_path / "release.7z"
    subprocess.run(
        [find_sevenzip(), "a", "-y", str(archive), "."],
        cwd=staging,
        check=True,
        capture_output=True,
    )

    fake = FakeAcgrip(archive)
    monkeypatch.setattr(
        "reeloom.server.subtitles.AcgripClient", lambda **kwargs: fake
    )

    database = FakeDatabase([config])
    run = Run(
        id="run-1",
        config_id=config.id,
        folder_name="Drop",
        state=RunState.ACQUIRING_SUBS,
        plan=Plan(identity=IDENTITY, moves=(video_move(1), video_move(2))),
    )
    database.runs[run.id] = run
    model = ScriptedModel(
        call("search_subtitles", keyword="Show"),
        call("select_release", attachment_id=99, reason="batch"),
    )
    service = SubtitleAcquisition(
        database, StubClients(model, None), tmp_path / "work"
    )

    result = await service.acquire(run, config, RunResult(moved=2))

    assert result.subtitles_acquired == 2
    assert (season / "Show S01E01.chs.ass").is_file()
    assert (season / "Show S01E02.chs.ass").is_file()
    assert fake.searches == ["Show"]
    # Publication went through the executor, so it is in the revert ledger.
    kinds = [item.move.kind for item in database.runs[run.id].executed_moves]
    assert kinds == [MoveKind.ACQUIRED_SUBTITLE, MoveKind.ACQUIRED_SUBTITLE]


@pytest.mark.skipif(find_sevenzip() is None, reason="7-Zip is not installed")
async def test_an_existing_subtitle_is_never_overwritten(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    inbound, library = roots
    season = library / LIBRARY_FOLDER / "S01"
    make_files(season, "Show S01E01.mkv", "Show S01E02.mkv")
    (season / "Show S01E02.chs.ass").write_text("mine", "utf-8")

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "[Group] Show - 02 [CHS].ass").write_text("这个国家", "utf-8")
    archive = tmp_path / "release.7z"
    subprocess.run(
        [find_sevenzip(), "a", "-y", str(archive), "."],
        cwd=staging,
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(
        "reeloom.server.subtitles.AcgripClient",
        lambda **kwargs: FakeAcgrip(archive),
    )

    database = FakeDatabase([config])
    run = Run(
        id="run-1",
        config_id=config.id,
        folder_name="Drop",
        state=RunState.ACQUIRING_SUBS,
        plan=Plan(identity=IDENTITY, moves=(video_move(1), video_move(2))),
    )
    database.runs[run.id] = run
    service = SubtitleAcquisition(
        database,
        StubClients(
            ScriptedModel(
                call("search_subtitles", keyword="Show"),
                call("select_release", attachment_id=99),
            ),
            None,
        ),
        tmp_path / "work",
    )

    result = await service.acquire(run, config, RunResult())

    # Episode 2 already had a subtitle, so it was never even wanted.
    assert (season / "Show S01E02.chs.ass").read_text() == "mine"
    assert result.subtitles_acquired == 0


async def test_giving_up_leaves_the_run_finished(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    _, library = roots
    make_files(library / LIBRARY_FOLDER / "S01", "Show S01E01.mkv")
    monkeypatch.setattr(
        "reeloom.server.subtitles.AcgripClient",
        lambda **kwargs: FakeAcgrip(tmp_path / "absent.7z"),
    )

    database = FakeDatabase([config])
    run = Run(
        id="run-1",
        config_id=config.id,
        folder_name="Drop",
        state=RunState.ACQUIRING_SUBS,
        plan=Plan(identity=IDENTITY, moves=(video_move(1),)),
    )
    database.runs[run.id] = run
    service = SubtitleAcquisition(
        database,
        StubClients(
            ScriptedModel(call("give_up", reason="nothing matches")), None
        ),
        tmp_path / "work",
    )

    result = await service.acquire(run, config, RunResult(moved=1))

    assert result.subtitles_acquired == 0
    assert result.subtitle_note == "未找到合适的字幕发布"


async def test_nothing_happens_when_every_episode_already_has_subtitles(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path
) -> None:
    _, library = roots
    make_files(
        library / LIBRARY_FOLDER / "S01",
        "Show S01E01.mkv",
        "Show S01E01.cht.srt",
    )
    database = FakeDatabase([config])
    run = make_run(database, config, 1)
    service = SubtitleAcquisition(database, StubClients(None, None), tmp_path)

    result = await service.acquire(run, config, RunResult(moved=1))

    assert result.subtitles_acquired == 0
    assert result.subtitle_note == ""


# ---- variant preference --------------------------------------------------


@pytest.mark.skipif(find_sevenzip() is None, reason="7-Zip is not installed")
async def test_a_dual_variant_batch_counts_episodes_not_files(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    """Regression: chs+cht pairs once produced a negative 仍缺字幕 count."""

    _, library = roots
    season = library / LIBRARY_FOLDER / "S01"
    make_files(season, "Show S01E01.mkv", "Show S01E02.mkv")
    archive = make_archive(
        tmp_path,
        "dual",
        {
            "[Group] Show - 01 [CHS].ass": SIMPLIFIED_TEXT,
            "[Group] Show - 01 [CHT].ass": TRADITIONAL_TEXT,
            "[Group] Show - 02 [CHS].ass": SIMPLIFIED_TEXT,
            "[Group] Show - 02 [CHT].ass": TRADITIONAL_TEXT,
        },
    )
    monkeypatch.setattr(
        "reeloom.server.subtitles.AcgripClient",
        lambda **kwargs: FakeAcgrip(archive),
    )

    database = FakeDatabase([config])
    run = make_run(database, config, 1, 2)
    service = SubtitleAcquisition(
        database,
        StubClients(
            ScriptedModel(
                call("search_subtitles", keyword="Show"),
                call("select_release", attachment_id=99),
            ),
            None,
        ),
        tmp_path / "work",
    )

    result = await service.acquire(run, config, RunResult(moved=2))

    assert result.subtitles_acquired == 4
    assert result.subtitle_note == ""
    assert (season / "Show S01E01.chs.ass").is_file()
    assert (season / "Show S01E02.cht.ass").is_file()


@pytest.mark.skipif(find_sevenzip() is None, reason="7-Zip is not installed")
async def test_partial_coverage_reports_missing_episodes(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    _, library = roots
    season = library / LIBRARY_FOLDER / "S01"
    make_files(season, "Show S01E01.mkv", "Show S01E02.mkv")
    archive = make_archive(
        tmp_path, "partial", {"[Group] Show - 01 [CHS].ass": SIMPLIFIED_TEXT}
    )
    monkeypatch.setattr(
        "reeloom.server.subtitles.AcgripClient",
        lambda **kwargs: FakeAcgrip(archive),
    )

    database = FakeDatabase([config])
    run = make_run(database, config, 1, 2)
    service = SubtitleAcquisition(
        database,
        StubClients(
            ScriptedModel(
                call("search_subtitles", keyword="Show"),
                call("select_release", attachment_id=99),
            ),
            None,
        ),
        tmp_path / "work",
    )

    result = await service.acquire(run, config, RunResult(moved=2))

    assert result.subtitles_acquired == 1
    assert result.subtitle_note == "1 集仍缺字幕"


@pytest.mark.skipif(find_sevenzip() is None, reason="7-Zip is not installed")
async def test_a_non_preferred_release_is_retried_with_feedback(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    config = replace(config, subtitle_variant=SubtitleVariant.CHT)
    _, library = roots
    season = library / LIBRARY_FOLDER / "S01"
    make_files(season, "Show S01E01.mkv")
    fake = FakeAcgrip(
        {
            99: make_archive(
                tmp_path, "chs", {"[Group] Show - 01 [CHS].ass": SIMPLIFIED_TEXT}
            ),
            100: make_archive(
                tmp_path, "cht", {"[Group] Show - 01 [CHT].ass": TRADITIONAL_TEXT}
            ),
        }
    )
    monkeypatch.setattr(
        "reeloom.server.subtitles.AcgripClient", lambda **kwargs: fake
    )

    database = FakeDatabase([config])
    run = make_run(database, config, 1)
    model = ScriptedModel(
        call("search_subtitles", keyword="Show"),
        call("select_release", attachment_id=99),
        call("select_release", attachment_id=100),
    )
    service = SubtitleAcquisition(
        database, StubClients(model, None), tmp_path / "work"
    )

    result = await service.acquire(run, config, RunResult(moved=1))

    assert fake.downloaded == [99, 100]
    assert result.subtitles_acquired == 1
    assert result.subtitle_note == ""
    assert (season / "Show S01E01.cht.ass").is_file()
    assert not (season / "Show S01E01.chs.ass").exists()
    # The model was told what the first download contained.
    feedback = [
        message["content"]
        for message in model.seen[-1]
        if message["role"] == "user"
    ]
    assert any("[CHS].ass: chs" in text for text in feedback)


@pytest.mark.skipif(find_sevenzip() is None, reason="7-Zip is not installed")
async def test_reselecting_the_same_attachment_accepts_it(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    config = replace(config, subtitle_variant=SubtitleVariant.CHT)
    _, library = roots
    season = library / LIBRARY_FOLDER / "S01"
    make_files(season, "Show S01E01.mkv")
    fake = FakeAcgrip(
        make_archive(
            tmp_path, "chs", {"[Group] Show - 01 [CHS].ass": SIMPLIFIED_TEXT}
        )
    )
    monkeypatch.setattr(
        "reeloom.server.subtitles.AcgripClient", lambda **kwargs: fake
    )

    database = FakeDatabase([config])
    run = make_run(database, config, 1)
    service = SubtitleAcquisition(
        database,
        StubClients(
            ScriptedModel(
                call("search_subtitles", keyword="Show"),
                call("select_release", attachment_id=99),
                call("select_release", attachment_id=99, reason="only release"),
            ),
            None,
        ),
        tmp_path / "work",
    )

    result = await service.acquire(run, config, RunResult(moved=1))

    # Accepted as-is: one download, published despite the cht preference.
    assert fake.downloaded == [99]
    assert result.subtitles_acquired == 1
    assert (season / "Show S01E01.chs.ass").is_file()


@pytest.mark.skipif(find_sevenzip() is None, reason="7-Zip is not installed")
async def test_exhausted_attempts_publish_the_best_coverage(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    config = replace(config, subtitle_variant=SubtitleVariant.CHT)
    _, library = roots
    season = library / LIBRARY_FOLDER / "S01"
    make_files(season, "Show S01E01.mkv", "Show S01E02.mkv")
    fake = FakeAcgrip(
        {
            99: make_archive(
                tmp_path, "one", {"[Group] Show - 01 [CHS].ass": SIMPLIFIED_TEXT}
            ),
            100: make_archive(
                tmp_path,
                "both",
                {
                    "[Group] Show - 01 [CHS].ass": SIMPLIFIED_TEXT,
                    "[Group] Show - 02 [CHS].ass": SIMPLIFIED_TEXT,
                },
            ),
            101: make_archive(
                tmp_path, "other", {"[Group] Show - 02 [CHS].ass": SIMPLIFIED_TEXT}
            ),
        }
    )
    monkeypatch.setattr(
        "reeloom.server.subtitles.AcgripClient", lambda **kwargs: fake
    )

    database = FakeDatabase([config])
    run = make_run(database, config, 1, 2)
    service = SubtitleAcquisition(
        database,
        StubClients(
            ScriptedModel(
                call("search_subtitles", keyword="Show"),
                call("select_release", attachment_id=99),
                call("select_release", attachment_id=100),
                call("select_release", attachment_id=101),
            ),
            None,
        ),
        tmp_path / "work",
    )

    result = await service.acquire(run, config, RunResult(moved=2))

    # The download budget ran out; the widest coverage was published anyway.
    assert fake.downloaded == [99, 100, 101]
    assert result.subtitles_acquired == 2
    assert result.subtitle_note == ""
    assert (season / "Show S01E01.chs.ass").is_file()
    assert (season / "Show S01E02.chs.ass").is_file()


@pytest.mark.skipif(find_sevenzip() is None, reason="7-Zip is not installed")
async def test_giving_up_after_a_download_publishes_nothing(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    config = replace(config, subtitle_variant=SubtitleVariant.CHT)
    _, library = roots
    season = library / LIBRARY_FOLDER / "S01"
    make_files(season, "Show S01E01.mkv")
    fake = FakeAcgrip(
        make_archive(
            tmp_path, "chs", {"[Group] Show - 01 [CHS].ass": SIMPLIFIED_TEXT}
        )
    )
    monkeypatch.setattr(
        "reeloom.server.subtitles.AcgripClient", lambda **kwargs: fake
    )

    database = FakeDatabase([config])
    run = make_run(database, config, 1)
    service = SubtitleAcquisition(
        database,
        StubClients(
            ScriptedModel(
                call("search_subtitles", keyword="Show"),
                call("select_release", attachment_id=99),
                call("give_up", reason="wrong variant only"),
            ),
            None,
        ),
        tmp_path / "work",
    )

    result = await service.acquire(run, config, RunResult(moved=1))

    assert result.subtitles_acquired == 0
    assert result.subtitle_note == "未找到合适的字幕发布"
    assert not (season / "Show S01E01.chs.ass").exists()


@pytest.mark.skipif(find_sevenzip() is None, reason="7-Zip is not installed")
async def test_a_dud_archive_consumes_an_attempt_then_a_good_one_succeeds(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    _, library = roots
    season = library / LIBRARY_FOLDER / "S01"
    make_files(season, "Show S01E01.mkv")
    fake = FakeAcgrip(
        {
            99: make_archive(tmp_path, "dud", {"notes.txt": "no subtitles here"}),
            100: make_archive(
                tmp_path, "good", {"[Group] Show - 01 [CHS].ass": SIMPLIFIED_TEXT}
            ),
        }
    )
    monkeypatch.setattr(
        "reeloom.server.subtitles.AcgripClient", lambda **kwargs: fake
    )

    database = FakeDatabase([config])
    run = make_run(database, config, 1)
    model = ScriptedModel(
        call("search_subtitles", keyword="Show"),
        call("select_release", attachment_id=99),
        call("select_release", attachment_id=100),
    )
    service = SubtitleAcquisition(
        database, StubClients(model, None), tmp_path / "work"
    )

    result = await service.acquire(run, config, RunResult(moved=1))

    assert fake.downloaded == [99, 100]
    assert result.subtitles_acquired == 1
    assert result.subtitle_note == ""
    feedback = [
        message["content"]
        for message in model.seen[-1]
        if message["role"] == "user"
    ]
    assert any("archive_had_no_subtitles" in text for text in feedback)
