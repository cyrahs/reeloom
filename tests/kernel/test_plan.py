import json
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath

import pytest

from reeloom.kernel.candidates import Candidate, CandidateId, CandidateSnapshot
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.mapping import EpisodeCatalog, EpisodeSpan, MappingDraft
from reeloom.kernel.naming import SeriesIdentity, SubtitleVariant
from reeloom.kernel.plan import (
    CURRENT_PLAN_POLICY_VERSION,
    CURRENT_PLAN_SCHEMA_VERSION,
    PlanDraft,
    PlannedMove,
)


def series(title: str = "示例动画") -> SeriesIdentity:
    return SeriesIdentity.from_dict(
        {
            "title_zh_cn": title,
            "year": 2024,
            "tmdb_id": 42,
        }
    )


def snapshot(*candidate_ids: str) -> CandidateSnapshot:
    return CandidateSnapshot.create(
        [
            Candidate.from_dict(
                {
                    "id": candidate_id,
                    "kind": candidate_id.split(":", maxsplit=1)[0],
                    "display_name": f"candidate-{index}",
                }
            )
            for index, candidate_id in enumerate(candidate_ids, start=1)
        ]
    )


def video_move(
    source_id: str,
    *,
    title: str = "示例动画",
    episode: int = 1,
) -> PlannedMove:
    return PlannedMove.for_video(
        source_id=CandidateId.parse(source_id),
        series=series(title),
        span=EpisodeSpan(
            season=1,
            episode_start=episode,
            episode_end=episode,
        ),
        extension=".mkv",
    )


def mapping(
    candidates: CandidateSnapshot,
    *video_episodes: tuple[str, int],
    subtitles: tuple[tuple[str, str], ...] = (),
) -> MappingDraft:
    return MappingDraft.from_dict(
        {
            "videos": [
                {
                    "video_id": video_id,
                    "season": 1,
                    "episode_start": episode,
                    "episode_end": episode,
                }
                for video_id, episode in video_episodes
            ],
            "subtitles": [
                {
                    "subtitle_id": subtitle_id,
                    "video_id": video_id,
                }
                for subtitle_id, video_id in subtitles
            ],
        },
        candidates=candidates,
        catalog=EpisodeCatalog.from_counts({1: 100}),
    )


def test_behavior_fixture_builds_expected_immutable_plan() -> None:
    fixture_path = (
        Path(__file__).parents[1] / "fixtures" / "m0_plan_cases.json"
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert payload["provenance"]
    identity = SeriesIdentity.from_dict(payload["series"])
    candidates = CandidateSnapshot.create(
        Candidate.from_dict(item) for item in payload["candidates"]
    )
    moves: list[PlannedMove] = []
    video_payloads: list[dict[str, object]] = []
    subtitle_payloads: list[dict[str, object]] = []
    for item in payload["moves"]:
        span = EpisodeSpan(
            season=item["season"],
            episode_start=item["episode_start"],
            episode_end=item["episode_end"],
        )
        if item["kind"] == "video":
            video_payloads.append(
                {
                    "video_id": item["source_id"],
                    "season": item["season"],
                    "episode_start": item["episode_start"],
                    "episode_end": item["episode_end"],
                }
            )
            move = PlannedMove.for_video(
                source_id=CandidateId.parse(item["source_id"]),
                series=identity,
                span=span,
                extension=item["extension"],
            )
        else:
            subtitle_payloads.append(
                {
                    "subtitle_id": item["source_id"],
                    "video_id": item["video_id"],
                }
            )
            move = PlannedMove.for_subtitle(
                source_id=CandidateId.parse(item["source_id"]),
                video_id=CandidateId.parse(item["video_id"]),
                series=identity,
                span=span,
                variant=SubtitleVariant(item["variant"]),
                extension=item["extension"],
            )
        moves.append(move)

    episode_catalog = EpisodeCatalog.from_counts(
        {
            int(season): episode_count
            for season, episode_count in payload["episode_catalog"].items()
        }
    )
    validated_mapping = MappingDraft.from_dict(
        {
            "videos": video_payloads,
            "subtitles": subtitle_payloads,
        },
        candidates=candidates,
        catalog=episode_catalog,
    )
    plan = PlanDraft.create(
        moves,
        series=identity,
        mapping=validated_mapping,
        candidates=candidates,
    )

    assert plan.schema_version == CURRENT_PLAN_SCHEMA_VERSION
    assert plan.policy_version == CURRENT_PLAN_POLICY_VERSION
    assert plan.series == identity
    assert plan.mapping == validated_mapping
    assert tuple(str(move.destination) for move in plan.moves) == tuple(
        payload["expected_destinations"]
    )
    assert tuple(map(str, plan.unmapped_candidate_ids)) == tuple(
        payload["expected_unmapped"]
    )
    with pytest.raises(FrozenInstanceError):
        plan.moves = ()  # type: ignore[misc]


def test_planned_move_does_not_accept_a_caller_supplied_destination() -> None:
    with pytest.raises(TypeError):
        PlannedMove(  # type: ignore[call-arg]
            source_id=CandidateId.parse("video:1"),
            destination=PurePosixPath("../../chosen-by-agent"),
        )


def test_plan_rejects_duplicate_source() -> None:
    candidates = snapshot("video:1")
    validated_mapping = mapping(candidates, ("video:1", 1))

    with pytest.raises(DomainError) as raised:
        PlanDraft.create(
            [
                video_move("video:1", episode=1),
                video_move("video:1", episode=2),
            ],
            series=series(),
            mapping=validated_mapping,
            candidates=candidates,
        )

    assert raised.value.code is ErrorCode.DUPLICATE_PLAN_SOURCE
    assert raised.value.context == {"candidate_id": "video:1"}


def test_plan_rejects_exact_destination_collision() -> None:
    candidates = snapshot("video:1", "video:2")
    validated_mapping = mapping(
        candidates,
        ("video:1", 1),
        ("video:2", 2),
    )
    first = video_move("video:1", episode=1)
    second = video_move("video:2", episode=2)
    colliding_second = PlannedMove._from_compiled_destination(
        source_id=second.source_id,
        video_id=second.video_id,
        series=second.series,
        span=second.span,
        destination=first.destination,
    )

    with pytest.raises(DomainError) as raised:
        PlanDraft.create(
            [first, colliding_second],
            series=series(),
            mapping=validated_mapping,
            candidates=candidates,
        )

    assert raised.value.code is ErrorCode.DESTINATION_COLLISION


def test_plan_rejects_casefolded_cross_platform_collision() -> None:
    candidates = snapshot("video:1", "video:2")
    validated_mapping = mapping(
        candidates,
        ("video:1", 1),
        ("video:2", 2),
    )
    first = video_move("video:1", title="Anime", episode=1)
    second = video_move("video:2", title="Anime", episode=2)
    case_variant = PurePosixPath(
        *(part.replace("Anime", "anime") for part in first.destination.parts)
    )
    colliding_second = PlannedMove._from_compiled_destination(
        source_id=second.source_id,
        video_id=second.video_id,
        series=second.series,
        span=second.span,
        destination=case_variant,
    )

    with pytest.raises(DomainError) as raised:
        PlanDraft.create(
            [first, colliding_second],
            series=series("Anime"),
            mapping=validated_mapping,
            candidates=candidates,
        )

    assert raised.value.code is ErrorCode.DESTINATION_COLLISION


def test_same_variant_subtitles_are_disambiguated_by_stable_source_id() -> None:
    video_id = CandidateId.parse("video:1")
    first = PlannedMove.for_subtitle(
        source_id=CandidateId.parse("subtitle:1"),
        video_id=video_id,
        series=series(),
        span=EpisodeSpan(season=1, episode_start=1, episode_end=1),
        variant=SubtitleVariant.CHS,
        extension=".ass",
    )
    second = PlannedMove.for_subtitle(
        source_id=CandidateId.parse("subtitle:2"),
        video_id=video_id,
        series=series(),
        span=EpisodeSpan(season=1, episode_start=1, episode_end=1),
        variant=SubtitleVariant.CHS,
        extension=".ass",
    )

    candidates = snapshot("video:1", "subtitle:1", "subtitle:2")
    validated_mapping = mapping(
        candidates,
        ("video:1", 1),
        subtitles=(
            ("subtitle:1", "video:1"),
            ("subtitle:2", "video:1"),
        ),
    )
    plan = PlanDraft.create(
        [second, video_move("video:1"), first],
        series=series(),
        mapping=validated_mapping,
        candidates=candidates,
    )

    subtitle_names = tuple(
        move.destination.name
        for move in plan.moves
        if move.source_id.kind.value == "subtitle"
    )
    assert subtitle_names == (
        "示例动画 S01E01.chs.ass",
        "示例动画 S01E01.chs.1.ass",
    )


def test_unmapped_candidates_are_derived_from_validated_mapping() -> None:
    candidates = snapshot("video:1", "video:2", "subtitle:1")
    plan = PlanDraft.create(
        [video_move("video:1")],
        series=series(),
        mapping=mapping(candidates, ("video:1", 1)),
        candidates=candidates,
    )

    assert tuple(map(str, plan.unmapped_candidate_ids)) == (
        "video:2",
        "subtitle:1",
    )


def test_every_mapped_candidate_must_have_a_move() -> None:
    candidates = snapshot("video:1", "video:2")
    validated_mapping = mapping(
        candidates,
        ("video:1", 1),
        ("video:2", 2),
    )

    with pytest.raises(DomainError) as raised:
        PlanDraft.create(
            [video_move("video:1")],
            series=series(),
            mapping=validated_mapping,
            candidates=candidates,
        )

    assert raised.value.code is ErrorCode.MISSING_PLAN_CANDIDATES
    assert raised.value.context == {"candidate_ids": ("video:2",)}


def test_plan_rejects_source_outside_candidate_snapshot() -> None:
    mapping_candidates = snapshot("video:99")
    validated_mapping = mapping(mapping_candidates, ("video:99", 1))

    with pytest.raises(DomainError) as raised:
        PlanDraft.create(
            [video_move("video:99")],
            series=series(),
            mapping=validated_mapping,
            candidates=snapshot("video:1"),
        )

    assert raised.value.code is ErrorCode.UNKNOWN_CANDIDATE_ID
    assert raised.value.context == {"candidate_id": "video:99"}


def test_unmapped_subtitle_cannot_be_promoted_to_a_move() -> None:
    candidates = snapshot("video:1", "subtitle:1")
    subtitle_move = PlannedMove.for_subtitle(
        source_id=CandidateId.parse("subtitle:1"),
        video_id=CandidateId.parse("video:1"),
        series=series(),
        span=EpisodeSpan(season=1, episode_start=1, episode_end=1),
        variant=SubtitleVariant.CHS,
        extension=".ass",
    )

    with pytest.raises(DomainError) as raised:
        PlanDraft.create(
            [video_move("video:1"), subtitle_move],
            series=series(),
            mapping=mapping(candidates, ("video:1", 1)),
            candidates=candidates,
        )

    assert raised.value.code is ErrorCode.PLAN_MAPPING_MISMATCH
    assert raised.value.context == {
        "candidate_id": "subtitle:1",
        "reason": "not_mapped",
    }


def test_plan_move_metadata_must_match_mapping() -> None:
    candidates = snapshot("video:1")

    with pytest.raises(DomainError) as raised:
        PlanDraft.create(
            [video_move("video:1", episode=2)],
            series=series(),
            mapping=mapping(candidates, ("video:1", 1)),
            candidates=candidates,
        )

    assert raised.value.code is ErrorCode.PLAN_MAPPING_MISMATCH
    assert raised.value.context == {
        "candidate_id": "video:1",
        "reason": "metadata",
    }


def test_subtitle_move_must_keep_its_mapping_association() -> None:
    candidates = snapshot("video:1", "video:2", "subtitle:1")
    validated_mapping = mapping(
        candidates,
        ("video:1", 1),
        ("video:2", 2),
        subtitles=(("subtitle:1", "video:1"),),
    )
    wrong_subtitle_move = PlannedMove.for_subtitle(
        source_id=CandidateId.parse("subtitle:1"),
        video_id=CandidateId.parse("video:2"),
        series=series(),
        span=EpisodeSpan(season=1, episode_start=1, episode_end=1),
        variant=SubtitleVariant.CHS,
        extension=".ass",
    )

    with pytest.raises(DomainError) as raised:
        PlanDraft.create(
            [
                video_move("video:1", episode=1),
                video_move("video:2", episode=2),
                wrong_subtitle_move,
            ],
            series=series(),
            mapping=validated_mapping,
            candidates=candidates,
        )

    assert raised.value.code is ErrorCode.PLAN_MAPPING_MISMATCH
    assert raised.value.context == {
        "candidate_id": "subtitle:1",
        "reason": "metadata",
    }


def test_plan_rejects_move_from_another_series() -> None:
    candidates = snapshot("video:1")

    with pytest.raises(DomainError) as raised:
        PlanDraft.create(
            [video_move("video:1", title="另一个动画")],
            series=series(),
            mapping=mapping(candidates, ("video:1", 1)),
            candidates=candidates,
        )

    assert raised.value.code is ErrorCode.PLAN_MAPPING_MISMATCH


@pytest.mark.parametrize(
    ("factory", "source_id"),
    [
        ("video", "subtitle:1"),
        ("subtitle", "video:1"),
    ],
)
def test_move_factory_enforces_source_kind(
    factory: str,
    source_id: str,
) -> None:
    if factory == "video":
        call = lambda: PlannedMove.for_video(
            source_id=CandidateId.parse(source_id),
            series=series(),
            span=EpisodeSpan(season=1, episode_start=1, episode_end=1),
            extension=".mkv",
        )
    else:
        call = lambda: PlannedMove.for_subtitle(
            source_id=CandidateId.parse(source_id),
            video_id=CandidateId.parse("video:2"),
            series=series(),
            span=EpisodeSpan(season=1, episode_start=1, episode_end=1),
            variant=SubtitleVariant.CHS,
            extension=".ass",
        )

    with pytest.raises(DomainError) as raised:
        call()

    assert raised.value.code is ErrorCode.CANDIDATE_KIND_MISMATCH


def test_plan_does_not_accept_a_caller_selected_policy_version() -> None:
    candidates = snapshot("video:1")
    with pytest.raises(TypeError):
        PlanDraft.create(  # type: ignore[call-arg]
            [video_move("video:1")],
            series=series(),
            mapping=mapping(candidates, ("video:1", 1)),
            candidates=candidates,
            policy_version="agent-selected-policy",
        )


def test_plan_order_is_deterministic() -> None:
    candidates = snapshot("subtitle:1", "video:2", "video:1")
    plan = PlanDraft.create(
        [
            video_move("video:2", episode=2),
            video_move("video:1", episode=1),
        ],
        series=series(),
        mapping=mapping(
            candidates,
            ("video:1", 1),
            ("video:2", 2),
        ),
        candidates=candidates,
    )

    assert tuple(str(move.source_id) for move in plan.moves) == (
        "video:1",
        "video:2",
    )
    assert tuple(map(str, plan.unmapped_candidate_ids)) == ("subtitle:1",)
