from pathlib import PurePosixPath

import pytest

from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.movie import (
    MovieMappingDraft,
    compile_movie_plan_draft,
)
from reeloom.kernel.naming import MovieIdentity, SubtitleVariant
from reeloom.kernel.scanner import ScannedFile, build_candidate_snapshot


def _snapshot():
    return build_candidate_snapshot(
        (
            ScannedFile(
                PurePosixPath("feature.mkv"),
                CandidateKind.VIDEO,
                10,
                1,
                11,
                12,
                13,
            ),
            ScannedFile(
                PurePosixPath("extra.mp4"),
                CandidateKind.VIDEO,
                20,
                1,
                21,
                22,
                23,
            ),
            ScannedFile(
                PurePosixPath("a.srt"),
                CandidateKind.SUBTITLE,
                5,
                1,
                31,
                32,
                33,
                "a" * 64,
            ),
            ScannedFile(
                PurePosixPath("b.srt"),
                CandidateKind.SUBTITLE,
                6,
                1,
                41,
                42,
                43,
                "b" * 64,
            ),
        )
    )


def test_movie_mapping_compiles_one_feature_and_stable_subtitles() -> None:
    snapshot = _snapshot()
    mapping = MovieMappingDraft.from_dict(
        {
                "video_id": "video:2",
            "subtitle_ids": ["subtitle:2", "subtitle:1"],
        },
        candidates=snapshot.candidates,
    )
    draft = compile_movie_plan_draft(
        movie=MovieIdentity("电影：测试", 2024, 99),
        mapping=mapping,
        candidates=snapshot,
        subtitle_variants=(
            (mapping.subtitle_ids[0], SubtitleVariant.CHS),
            (mapping.subtitle_ids[1], SubtitleVariant.CHS),
        ),
    )

    assert [str(item.destination) for item in draft.moves] == [
        "电影 测试 (2024) {tmdb-99}/电影 测试 (2024).mkv",
        "电影 测试 (2024) {tmdb-99}/电影 测试 (2024).chs.1.srt",
        "电影 测试 (2024) {tmdb-99}/电影 测试 (2024).chs.2.srt",
    ]
    assert tuple(map(str, draft.unmapped_candidate_ids)) == ("video:1",)


def test_movie_mapping_rejects_missing_video_and_extra_keys() -> None:
    snapshot = _snapshot()
    for payload in (
        {"video_id": "subtitle:1", "subtitle_ids": []},
        {"video_id": "video:1", "subtitle_ids": [], "path": "/tmp"},
    ):
        with pytest.raises(DomainError):
            MovieMappingDraft.from_dict(
                payload,
                candidates=snapshot.candidates,
            )


def test_movie_draft_requires_every_selected_subtitle_variant() -> None:
    snapshot = _snapshot()
    mapping = MovieMappingDraft.from_dict(
        {"video_id": "video:1", "subtitle_ids": ["subtitle:1"]},
        candidates=snapshot.candidates,
    )

    with pytest.raises(DomainError) as raised:
        compile_movie_plan_draft(
            movie=MovieIdentity("电影", 2024, 99),
            mapping=mapping,
            candidates=snapshot,
            subtitle_variants=(),
        )

    assert raised.value.code is ErrorCode.SUBTITLE_VARIANT_REQUIRED
