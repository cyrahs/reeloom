from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from pathlib import PurePosixPath

import pytest

from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.naming import SeriesIdentity, SubtitleVariant
from reeloom.kernel.rename_plan import (
    RenamePlan,
    RootBinding,
    compile_plan_draft,
    verify_plan_bytes,
)
from reeloom.kernel.scanner import ScannedFile, build_candidate_snapshot
from reeloom.kernel.tmdb import TmdbWorkType

_CREATED_AT = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _snapshot(*, video_inode: int = 11):
    return build_candidate_snapshot(
        (
            ScannedFile(
                relative_path=PurePosixPath("release/episode.mkv"),
                kind=CandidateKind.VIDEO,
                size_bytes=5,
                device=1,
                inode=video_inode,
                mtime_ns=100,
                ctime_ns=101,
            ),
            ScannedFile(
                relative_path=PurePosixPath("release/episode.ass"),
                kind=CandidateKind.SUBTITLE,
                size_bytes=8,
                device=1,
                inode=12,
                mtime_ns=102,
                ctime_ns=103,
                sample_digest="a" * 64,
            ),
            ScannedFile(
                relative_path=PurePosixPath("release/unmapped.mkv"),
                kind=CandidateKind.VIDEO,
                size_bytes=9,
                device=1,
                inode=13,
                mtime_ns=104,
                ctime_ns=105,
            ),
        )
    )


def _mapping(snapshot) -> MappingDraft:
    return MappingDraft.from_dict(
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
        candidates=snapshot.candidates,
        catalog=EpisodeCatalog.from_counts({1: 12}),
    )


def _series() -> SeriesIdentity:
    return SeriesIdentity(
        title_zh_cn="正确动画",
        year=2024,
        tmdb_id=200,
    )


def _root(path: str, *, inode: int) -> RootBinding:
    return RootBinding(
        path=PurePosixPath(path),
        device=1,
        inode=inode,
    )


def _plan(
    *,
    video_inode: int = 11,
    work_type: TmdbWorkType = TmdbWorkType.ANIME,
    created_at: datetime = _CREATED_AT,
) -> RenamePlan:
    snapshot = _snapshot(video_inode=video_inode)
    variants = (
        (
            snapshot.records[2].candidate.id,
            SubtitleVariant.CHS,
        ),
    )
    draft = compile_plan_draft(
        series=_series(),
        mapping=_mapping(snapshot),
        candidates=snapshot,
        subtitle_variants=variants,
    )
    return RenamePlan.create(
        run_id="run-m5",
        work_type=work_type,
        created_at=created_at,
        source_root=_root("/archive/incoming", inode=20),
        output_root=_root("/archive/anime", inode=21),
        candidate_snapshot=snapshot,
        subtitle_variants=variants,
        draft=draft,
        checked_destinations=tuple(
            move.destination for move in draft.moves
        ),
    )


def test_compiler_produces_canonical_immutable_plan_and_preview() -> None:
    plan = _plan()

    assert plan.plan_hash.startswith("sha256:")
    assert verify_plan_bytes(plan.canonical_bytes(), plan.plan_hash)
    assert plan.canonical_bytes() == _plan().canonical_bytes()
    assert plan.plan_hash == _plan().plan_hash
    assert tuple(
        str(move.destination) for move in plan.draft.moves
    ) == (
        "正确动画 (2024) {tmdb-200}/S01/正确动画 S01E02.mkv",
        "正确动画 (2024) {tmdb-200}/S01/正确动画 S01E02.chs.ass",
    )
    assert tuple(
        str(item.candidate_id) for item in plan.preview.unmapped
    ) == ("video:2",)
    assert all(
        not move.destination.is_absolute()
        and ".." not in move.destination.parts
        and (
            plan.output_root.path / move.destination
        ).is_relative_to(plan.output_root.path)
        for move in plan.draft.moves
    )


def test_changing_any_source_identity_changes_snapshot_and_plan_hash() -> None:
    original = _plan(video_inode=11)
    changed = _plan(video_inode=99)

    assert original.candidate_snapshot_id != changed.candidate_snapshot_id
    assert original.plan_hash != changed.plan_hash


def test_trusted_work_type_is_bound_into_the_plan_hash() -> None:
    anime = _plan(work_type=TmdbWorkType.ANIME)
    television = _plan(work_type=TmdbWorkType.TV_SERIES)

    assert anime.plan_hash != television.plan_hash


def test_plan_rejects_datetime_without_a_utc_offset() -> None:
    class MissingOffset(tzinfo):
        def utcoffset(self, value: datetime | None) -> None:
            del value
            return None

    with pytest.raises(DomainError) as raised:
        _plan(
            created_at=datetime(
                2026,
                7,
                23,
                12,
                0,
                tzinfo=MissingOffset(),
            )
        )

    assert raised.value.code is ErrorCode.INVALID_FIELD_TYPE


def test_tampering_with_canonical_bytes_invalidates_hash() -> None:
    plan = _plan()
    tampered = plan.canonical_bytes().replace(
        b'"episode_start":2',
        b'"episode_start":3',
    )

    assert tampered != plan.canonical_bytes()
    assert not verify_plan_bytes(tampered, plan.plan_hash)


def test_plan_rejects_unchecked_destination() -> None:
    snapshot = _snapshot()
    variants = (
        (
            snapshot.records[2].candidate.id,
            SubtitleVariant.CHS,
        ),
    )
    draft = compile_plan_draft(
        series=_series(),
        mapping=_mapping(snapshot),
        candidates=snapshot,
        subtitle_variants=variants,
    )

    with pytest.raises(DomainError) as raised:
        RenamePlan.create(
            run_id="run-m5",
            work_type=TmdbWorkType.ANIME,
            created_at=_CREATED_AT,
            source_root=_root("/archive/incoming", inode=20),
            output_root=_root("/archive/anime", inode=21),
            candidate_snapshot=snapshot,
            subtitle_variants=variants,
            draft=draft,
            checked_destinations=(draft.moves[0].destination,),
        )

    assert raised.value.code is ErrorCode.PLAN_PREFLIGHT_MISMATCH


def test_plan_requires_the_exact_checked_destination_spelling() -> None:
    snapshot = _snapshot()
    variants = (
        (
            snapshot.records[2].candidate.id,
            SubtitleVariant.CHS,
        ),
    )
    draft = compile_plan_draft(
        series=_series(),
        mapping=_mapping(snapshot),
        candidates=snapshot,
        subtitle_variants=variants,
    )
    checked = tuple(
        PurePosixPath(
            *(part.replace("正确动画", "正確動畫") for part in path.parts)
        )
        for path in (
            move.destination for move in draft.moves
        )
    )

    with pytest.raises(DomainError) as raised:
        RenamePlan.create(
            run_id="run-m5",
            work_type=TmdbWorkType.ANIME,
            created_at=_CREATED_AT,
            source_root=_root("/archive/incoming", inode=20),
            output_root=_root("/archive/anime", inode=21),
            candidate_snapshot=snapshot,
            subtitle_variants=variants,
            draft=draft,
            checked_destinations=checked,
        )

    assert raised.value.code is ErrorCode.PLAN_PREFLIGHT_MISMATCH


def test_plan_requires_complete_source_identity() -> None:
    snapshot = build_candidate_snapshot(
        (
            ScannedFile(
                relative_path=PurePosixPath("episode.mkv"),
                kind=CandidateKind.VIDEO,
                size_bytes=5,
            ),
        )
    )
    mapping = MappingDraft.from_dict(
        {
            "videos": [
                {
                    "video_id": "video:1",
                    "season": 1,
                    "episode_start": 1,
                    "episode_end": 1,
                }
            ],
            "subtitles": [],
        },
        candidates=snapshot.candidates,
        catalog=EpisodeCatalog.from_counts({1: 1}),
    )
    draft = compile_plan_draft(
        series=_series(),
        mapping=mapping,
        candidates=snapshot,
        subtitle_variants=(),
    )

    with pytest.raises(DomainError) as raised:
        RenamePlan.create(
            run_id="run-m5",
            work_type=TmdbWorkType.ANIME,
            created_at=_CREATED_AT,
            source_root=_root("/archive/incoming", inode=20),
            output_root=_root("/archive/anime", inode=21),
            candidate_snapshot=snapshot,
            subtitle_variants=(),
            draft=draft,
            checked_destinations=tuple(
                move.destination for move in draft.moves
            ),
        )

    assert raised.value.code is ErrorCode.INCOMPLETE_SOURCE_IDENTITY
