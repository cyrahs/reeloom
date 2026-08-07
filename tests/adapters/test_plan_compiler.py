from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reeloom.adapters.filesystem import (
    FilesystemPlanCompiler,
    FilesystemPlanCompilerV2,
    FilesystemScanner,
)
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.forward_execution import RenamePlanV2
from reeloom.kernel.movie import MovieMappingDraft
from reeloom.kernel.movie_forward_execution import MovieRenamePlanV2
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.naming import MovieIdentity, SeriesIdentity, SubtitleVariant
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.watcher import NoFollowWatcher

_CREATED_AT = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _setup(tmp_path: Path):
    source_path = tmp_path / "incoming"
    output_path = tmp_path / "anime"
    source_path.mkdir()
    output_path.mkdir()
    (source_path / "episode.mkv").write_bytes(b"video")
    (source_path / "episode.ass").write_bytes("简体字幕".encode())
    scan = FilesystemScanner().scan(AuthorizedRoot.create(source_path))
    mapping = MappingDraft.from_dict(
        {
            "videos": [
                {
                    "video_id": "video:1",
                    "season": 1,
                    "episode_start": 2,
                    "episode_end": 2,
                }
            ],
            "subtitles": [
                {
                    "subtitle_id": "subtitle:1",
                    "video_id": "video:1",
                }
            ],
        },
        candidates=scan.snapshot.candidates,
        catalog=EpisodeCatalog.from_counts({1: 12}),
    )
    compiler = FilesystemPlanCompiler(
        scan=scan,
        output_root=AuthorizedRoot.create(output_path),
    )
    return source_path, output_path, mapping, compiler


def _compile(
    compiler: FilesystemPlanCompiler,
    mapping: MappingDraft,
):
    return compiler.compile(
        run_id="run-m5",
        work_type=TmdbWorkType.ANIME,
        series=SeriesIdentity(
            title_zh_cn="正确动画",
            year=2024,
            tmdb_id=200,
        ),
        mapping=mapping,
        subtitle_variants=(
            (
                compiler.scan.snapshot.records[1].candidate.id,
                SubtitleVariant.CHS,
            ),
        ),
        created_at=_CREATED_AT,
    )


def test_plan_compiler_is_a_read_only_dry_run(tmp_path: Path) -> None:
    source, output, mapping, compiler = _setup(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    plan = _compile(compiler, mapping)

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert tuple(output.iterdir()) == ()
    assert (source / "episode.mkv").read_bytes() == b"video"
    assert plan.output_root.path.as_posix() == output.as_posix()


def test_v2_plan_compiler_uses_semantic_identity_and_path_only_roots(
    tmp_path: Path,
) -> None:
    source, output, mapping, legacy = _setup(tmp_path)
    semantic = NoFollowWatcher().scan(
        AuthorizedRoot.create(source)
    ).semantic_snapshot
    compiler = FilesystemPlanCompilerV2(
        scan=legacy.scan,
        semantic_snapshot=semantic,
        output_root=AuthorizedRoot.create(output),
        config_revision=9,
        watch_id="watch-anime",
    )

    plan = compiler.compile(
        run_id="run-m14-plan-only",
        work_type=TmdbWorkType.ANIME,
        series=SeriesIdentity("正确动画", 2024, 200),
        mapping=mapping,
        subtitle_variants=(
            (semantic.sources[1].candidate_id, SubtitleVariant.CHS),
        ),
        created_at=_CREATED_AT,
    )

    assert isinstance(plan, RenamePlanV2)
    assert plan.candidate_snapshot_id == semantic.snapshot_id
    assert plan.source_root.payload() == {"path": source.as_posix()}
    assert plan.output_root.payload() == {"path": output.as_posix()}
    assert tuple(output.iterdir()) == ()


def test_v2_plan_compiler_supports_movie_without_stat_identity(
    tmp_path: Path,
) -> None:
    source, output, _mapping, legacy = _setup(tmp_path)
    semantic = NoFollowWatcher().scan(
        AuthorizedRoot.create(source)
    ).semantic_snapshot
    compiler = FilesystemPlanCompilerV2(
        scan=legacy.scan,
        semantic_snapshot=semantic,
        output_root=AuthorizedRoot.create(output),
        config_revision=10,
        watch_id="watch-movie",
    )
    mapping = MovieMappingDraft.from_dict(
        {"video_id": "video:1", "subtitle_ids": ["subtitle:1"]},
        candidates=semantic.candidates,
    )

    plan = compiler.compile_movie(
        run_id="run-movie-v2",
        movie=MovieIdentity("正确电影", 2025, 300),
        mapping=mapping,
        subtitle_variants=(
            (semantic.sources[1].candidate_id, SubtitleVariant.CHS),
        ),
        created_at=_CREATED_AT,
    )

    assert isinstance(plan, MovieRenamePlanV2)
    assert plan.source_root.payload() == {"path": source.as_posix()}
    assert b'"inode"' not in plan.canonical_bytes()
    assert tuple(output.iterdir()) == ()


def test_plan_compiler_rejects_an_existing_destination(
    tmp_path: Path,
) -> None:
    _, output, mapping, compiler = _setup(tmp_path)
    plan = _compile(compiler, mapping)
    destination = output / Path(plan.draft.moves[0].destination)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")

    with pytest.raises(DomainError) as raised:
        _compile(compiler, mapping)

    assert raised.value.code is ErrorCode.DESTINATION_COLLISION


def test_plan_compiler_rejects_a_symlinked_destination_parent(
    tmp_path: Path,
) -> None:
    _, output, mapping, compiler = _setup(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    series_root = output / "正确动画 (2024) {tmdb-200}"
    series_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(DomainError) as raised:
        _compile(compiler, mapping)

    assert raised.value.code is ErrorCode.SYMLINK_NOT_ALLOWED


def test_plan_compiler_rejects_output_root_identity_drift(
    tmp_path: Path,
) -> None:
    _, output, mapping, compiler = _setup(tmp_path)
    output.rename(tmp_path / "original-output")
    output.mkdir()

    with pytest.raises(DomainError) as raised:
        _compile(compiler, mapping)

    assert raised.value.code is ErrorCode.SCAN_FAILED


def test_plan_compiler_closes_new_directory_when_fstat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, output, mapping, compiler = _setup(tmp_path)
    series_root = output / "正确动画 (2024) {tmdb-200}"
    series_root.mkdir()
    real_fstat = os.fstat
    real_close = os.close
    calls = 0
    failed_fd: int | None = None
    closed_fds: set[int] = set()

    def fail_second_fstat(file_descriptor: int) -> os.stat_result:
        nonlocal calls, failed_fd
        calls += 1
        if calls == 2:
            failed_fd = file_descriptor
            raise OSError("injected fstat failure")
        return real_fstat(file_descriptor)

    def track_close(file_descriptor: int) -> None:
        closed_fds.add(file_descriptor)
        real_close(file_descriptor)

    monkeypatch.setattr(os, "fstat", fail_second_fstat)
    monkeypatch.setattr(os, "close", track_close)

    with pytest.raises(DomainError) as raised:
        _compile(compiler, mapping)

    assert raised.value.code is ErrorCode.SCAN_FAILED
    assert failed_fd in closed_fds
