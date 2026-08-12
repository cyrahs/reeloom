from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from reeloom.adapters.ffprobe import Probe
from reeloom.adapters.llm import ModelReply
from reeloom.models import (
    FileKind,
    MediaIdentity,
    MediaType,
    Move,
    MoveKind,
    Plan,
    Root,
    Run,
    RunState,
    SnapshotFile,
    WatchConfig,
)
from reeloom.server.compare import ReplaceComparer, _parse_index
from reeloom.server.worker import NeedsAttention
from reeloom.trash import TRASH_DIR
from tests.conftest import make_files
from tests.fakes import FakeDatabase, FakeProber, FakeTmdb, ScriptedModel, StubClients

IDENTITY = MediaIdentity(MediaType.ANIME, 123, "Show", 2024)
FOLDER = "Show (2024) {tmdb-123}"
LIB_EP1 = f"{FOLDER}/S01/Show S01E01.mkv"


def incoming(episode: int, size: int) -> tuple[SnapshotFile, Move]:
    candidate_id = f"V{episode}"
    name = f"[BD] Show - {episode:02d}.mkv"
    snapshot = SnapshotFile(candidate_id, name, FileKind.VIDEO, size)
    move = Move(
        kind=MoveKind.MEDIA,
        source_root=Root.INBOUND,
        source_path=f"Drop/{name}",
        dest_root=Root.LIBRARY,
        dest_path=f"{FOLDER}/S01/Show S01E{episode:02d}.mkv",
        candidate_id=candidate_id,
    )
    return snapshot, move


def build_run(*pairs: tuple[SnapshotFile, Move]) -> Run:
    return Run(
        id="run-1",
        config_id="00000000-0000-0000-0000-000000000001",
        folder_name="Drop",
        state=RunState.COMPARING,
        snapshot=tuple(pair[0] for pair in pairs),
        plan=Plan(identity=IDENTITY, moves=tuple(pair[1] for pair in pairs)),
    )


def build_comparer(
    config: WatchConfig,
    run: Run,
    *,
    model: ScriptedModel | None = None,
    prober: FakeProber | None = None,
) -> tuple[ReplaceComparer, FakeDatabase]:
    database = FakeDatabase([config])
    database.runs[run.id] = run
    clients = StubClients(model or ScriptedModel(), FakeTmdb())
    comparer = ReplaceComparer(
        database, clients, prober=prober or FakeProber()
    )
    return comparer, database


@pytest.fixture
def rconfig(config: WatchConfig) -> WatchConfig:
    return replace(config, replace_enabled=True)


async def test_nothing_existing_changes_nothing(rconfig: WatchConfig) -> None:
    run = build_run(incoming(1, 100))
    comparer, database = build_comparer(rconfig, run)

    assert await comparer.compare(run, rconfig) is None
    stored = database.runs[run.id].extra["replace"]
    assert stored["groups"][0]["verdict"] == "import"


async def test_clear_upgrade_returns_an_augmented_plan(
    rconfig: WatchConfig, roots: tuple[Path, Path]
) -> None:
    _, library = roots
    make_files(library, LIB_EP1, size=16)
    run = build_run(incoming(1, 64))
    comparer, database = build_comparer(rconfig, run)

    augmented = await comparer.compare(run, rconfig)

    assert augmented is not None
    assert [move.kind for move in augmented.moves] == [
        MoveKind.TRASH_REPLACED,
        MoveKind.MEDIA,
    ]
    assert augmented.moves[0].source_path == LIB_EP1
    assert augmented.moves[0].dest_root is Root.INBOUND
    assert augmented.moves[0].dest_path == f"{TRASH_DIR}/run-1/library/{LIB_EP1}"


async def test_gray_zone_parks_for_confirmation(
    rconfig: WatchConfig, roots: tuple[Path, Path]
) -> None:
    _, library = roots
    make_files(library, LIB_EP1, size=100)
    run = build_run(incoming(1, 110))
    comparer, database = build_comparer(rconfig, run)

    with pytest.raises(NeedsAttention) as caught:
        await comparer.compare(run, rconfig)

    assert caught.value.code == "replace_confirmation"
    stored = database.runs[run.id].extra["replace"]
    assert stored["needs_confirmation"] is True


async def test_quality_veto_parks_a_big_download(
    rconfig: WatchConfig, roots: tuple[Path, Path]
) -> None:
    _, library = roots
    make_files(library, LIB_EP1, size=16)
    run = build_run(incoming(1, 64))
    prober = FakeProber(
        {
            "[BD] Show - 01.mkv": Probe(720, 1400.0, 1_000_000, "h264"),
            "Show S01E01.mkv": Probe(1080, 1400.0, 2_000_000, "hevc"),
        }
    )
    comparer, _ = build_comparer(rconfig, run, prober=prober)

    with pytest.raises(NeedsAttention):
        await comparer.compare(run, rconfig)
    assert prober.seen  # both sides actually sampled


async def test_stored_resolution_is_applied_when_facts_hold(
    rconfig: WatchConfig, roots: tuple[Path, Path]
) -> None:
    _, library = roots
    make_files(library, LIB_EP1, size=100)
    run = build_run(incoming(1, 110))
    comparer, database = build_comparer(rconfig, run)
    with pytest.raises(NeedsAttention):
        await comparer.compare(run, rconfig)

    # The API stores the user's choice next to the parked decision.
    parked = database.runs[run.id]
    await database.set_extra(
        run.id,
        {"replace": {**parked.extra["replace"], "resolution": "replace"}},
    )

    augmented = await comparer.compare(database.runs[run.id], rconfig)
    assert augmented is not None
    assert augmented.moves[0].kind is MoveKind.TRASH_REPLACED


async def test_stale_resolution_is_dropped_when_facts_change(
    rconfig: WatchConfig, roots: tuple[Path, Path]
) -> None:
    _, library = roots
    make_files(library, LIB_EP1, size=100)
    run = build_run(incoming(1, 110))
    comparer, database = build_comparer(rconfig, run)
    with pytest.raises(NeedsAttention):
        await comparer.compare(run, rconfig)

    parked = database.runs[run.id]
    await database.set_extra(
        run.id,
        {"replace": {**parked.extra["replace"], "resolution": "replace"}},
    )
    # The library changed while the run was parked — still a gray zone, but
    # not the comparison the user approved, so it parks again.
    (library / LIB_EP1).write_bytes(b"y" * 105)

    with pytest.raises(NeedsAttention):
        await comparer.compare(database.runs[run.id], rconfig)


async def test_keep_both_resolution_leaves_the_plan_alone(
    rconfig: WatchConfig, roots: tuple[Path, Path]
) -> None:
    _, library = roots
    make_files(library, LIB_EP1, size=100)
    run = build_run(incoming(1, 110))
    comparer, database = build_comparer(rconfig, run)
    with pytest.raises(NeedsAttention):
        await comparer.compare(run, rconfig)

    parked = database.runs[run.id]
    await database.set_extra(
        run.id,
        {"replace": {**parked.extra["replace"], "resolution": "keep_both"}},
    )

    assert await comparer.compare(database.runs[run.id], rconfig) is None


async def test_an_already_augmented_plan_is_left_alone(
    rconfig: WatchConfig,
) -> None:
    pair = incoming(1, 100)
    trash = Move(
        kind=MoveKind.TRASH_REPLACED,
        source_root=Root.LIBRARY,
        source_path=LIB_EP1,
        dest_root=Root.INBOUND,
        dest_path=f"{TRASH_DIR}/run-1/library/{LIB_EP1}",
    )
    run = build_run(pair)
    run = replace(run, plan=Plan(identity=IDENTITY, moves=(trash, pair[1])))
    comparer, _ = build_comparer(rconfig, run)

    assert await comparer.compare(run, rconfig) is None


# ---- extra directories ---------------------------------------------------


def extra_config(config: WatchConfig, extra: Path) -> WatchConfig:
    return replace(
        config, replace_enabled=True, replace_extra_dirs=(str(extra),)
    )


async def test_extra_dir_versions_are_found_by_name(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path
) -> None:
    extra = tmp_path / "anirss"
    make_files(extra, "Show/Season 1/Show - 01.mp4", size=16)
    rconfig = extra_config(config, extra)
    run = build_run(incoming(1, 64))
    comparer, database = build_comparer(rconfig, run)

    augmented = await comparer.compare(run, rconfig)

    assert augmented is not None
    trash = augmented.moves[0]
    assert trash.kind is MoveKind.TRASH_REPLACED
    assert trash.source_root is Root.EXTRA
    assert trash.extra_base == str(extra)
    assert trash.source_path == "Show/Season 1/Show - 01.mp4"


async def test_extra_dir_falls_back_to_the_model_for_fuzzy_names(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path
) -> None:
    extra = tmp_path / "anirss"
    make_files(extra, "败犬女主/Season 1/败犬女主 - 01.mp4", size=16)
    rconfig = extra_config(config, extra)
    run = build_run(incoming(1, 64))
    model = ScriptedModel(ModelReply(content='{"index": 0}'))
    comparer, database = build_comparer(rconfig, run, model=model)

    augmented = await comparer.compare(run, rconfig)

    assert augmented is not None
    assert augmented.moves[0].source_path == "败犬女主/Season 1/败犬女主 - 01.mp4"
    # The model saw folder names only — never a path.
    prompt = str(model.seen[0])
    assert "/anirss" not in prompt


async def test_garbage_model_output_skips_the_extra_dir(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path
) -> None:
    extra = tmp_path / "anirss"
    make_files(extra, "败犬女主/Season 1/败犬女主 - 01.mp4", size=16)
    rconfig = extra_config(config, extra)
    run = build_run(incoming(1, 64))
    model = ScriptedModel(ModelReply(content="the first folder looks right"))
    comparer, _ = build_comparer(rconfig, run, model=model)

    assert await comparer.compare(run, rconfig) is None


async def test_out_of_range_index_is_refused(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path
) -> None:
    extra = tmp_path / "anirss"
    make_files(extra, "败犬女主/Season 1/败犬女主 - 01.mp4", size=16)
    rconfig = extra_config(config, extra)
    run = build_run(incoming(1, 64))
    model = ScriptedModel(ModelReply(content='{"index": 7}'))
    comparer, _ = build_comparer(rconfig, run, model=model)

    assert await comparer.compare(run, rconfig) is None


def test_parse_index_is_defensive() -> None:
    assert _parse_index('{"index": 2}') == 2
    assert _parse_index('Sure! {"index": 0}') == 0
    assert _parse_index('{"index": null}') is None
    assert _parse_index('{"index": true}') is None
    assert _parse_index("nope") is None
    assert _parse_index('{"other": 1}') is None
