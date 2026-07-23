from dataclasses import FrozenInstanceError

import pytest

from reeloom.kernel.candidates import Candidate, CandidateSnapshot
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft


def candidate_snapshot() -> CandidateSnapshot:
    return CandidateSnapshot.create(
        [
            Candidate.from_dict(
                {
                    "id": "video:1",
                    "kind": "video",
                    "display_name": "episode-01.mkv",
                }
            ),
            Candidate.from_dict(
                {
                    "id": "video:2",
                    "kind": "video",
                    "display_name": "episode-02-03.mkv",
                }
            ),
            Candidate.from_dict(
                {
                    "id": "subtitle:1",
                    "kind": "subtitle",
                    "display_name": "episode-02-03.ass",
                }
            ),
        ]
    )


def episode_catalog() -> EpisodeCatalog:
    return EpisodeCatalog.from_counts({0: 2, 1: 4})


def valid_payload() -> dict[str, object]:
    return {
        "videos": [
            {
                "video_id": "video:1",
                "season": 1,
                "episode_start": 1,
                "episode_end": 1,
            },
            {
                "video_id": "video:2",
                "season": 1,
                "episode_start": 2,
                "episode_end": 3,
            },
        ],
        "subtitles": [
            {
                "subtitle_id": "subtitle:1",
                "video_id": "video:2",
            }
        ],
    }


def test_mapping_accepts_single_and_multi_episode_videos() -> None:
    draft = MappingDraft.from_dict(
        valid_payload(),
        candidates=candidate_snapshot(),
        catalog=episode_catalog(),
    )

    assert tuple(str(mapping.video_id) for mapping in draft.videos) == (
        "video:1",
        "video:2",
    )
    assert draft.videos[1].span.episodes == (2, 3)
    assert str(draft.subtitles[0].video_id) == "video:2"
    with pytest.raises(FrozenInstanceError):
        draft.videos[0].span.season = 2  # type: ignore[misc]


def test_mapping_schema_rejects_non_object_input() -> None:
    with pytest.raises(DomainError) as raised:
        MappingDraft.from_dict(
            ["videos", "subtitles"],
            candidates=candidate_snapshot(),
            catalog=episode_catalog(),
        )

    assert raised.value.code is ErrorCode.INVALID_FIELD_TYPE
    assert raised.value.context == {
        "field": "mapping",
        "expected": "object",
    }


@pytest.mark.parametrize(
    ("payload_update", "expected_keys"),
    [
        ({"destination": "/output/show"}, ("destination",)),
        (
            {
                "videos": [
                    {
                        "video_id": "video:1",
                        "season": 1,
                        "episode_start": 1,
                        "episode_end": 1,
                        "path": "/media/episode-01.mkv",
                    }
                ]
            },
            ("path",),
        ),
    ],
)
def test_mapping_rejects_extra_keys(
    payload_update: dict[str, object],
    expected_keys: tuple[str, ...],
) -> None:
    payload = valid_payload()
    payload.update(payload_update)

    with pytest.raises(DomainError) as raised:
        MappingDraft.from_dict(
            payload,
            candidates=candidate_snapshot(),
            catalog=episode_catalog(),
        )

    assert raised.value.code is ErrorCode.EXTRA_KEYS
    assert raised.value.context == {"keys": expected_keys}


def test_mapping_rejects_candidate_id_with_wrong_kind() -> None:
    payload = valid_payload()
    payload["videos"] = [
        {
            "video_id": "subtitle:1",
            "season": 1,
            "episode_start": 1,
            "episode_end": 1,
        }
    ]

    with pytest.raises(DomainError) as raised:
        MappingDraft.from_dict(
            payload,
            candidates=candidate_snapshot(),
            catalog=episode_catalog(),
        )

    assert raised.value.code is ErrorCode.CANDIDATE_KIND_MISMATCH


def test_mapping_rejects_id_outside_candidate_snapshot() -> None:
    payload = valid_payload()
    payload["videos"] = [
        {
            "video_id": "video:99",
            "season": 1,
            "episode_start": 1,
            "episode_end": 1,
        }
    ]

    with pytest.raises(DomainError) as raised:
        MappingDraft.from_dict(
            payload,
            candidates=candidate_snapshot(),
            catalog=episode_catalog(),
        )

    assert raised.value.code is ErrorCode.UNKNOWN_CANDIDATE_ID
    assert raised.value.context == {"candidate_id": "video:99"}


@pytest.mark.parametrize(
    ("season", "episode_start", "episode_end", "expected_code"),
    [
        (2, 1, 1, ErrorCode.SEASON_OUT_OF_BOUNDS),
        (1, 4, 5, ErrorCode.EPISODE_OUT_OF_BOUNDS),
        (1, 0, 1, ErrorCode.INVALID_EPISODE_RANGE),
        (1, 3, 2, ErrorCode.INVALID_EPISODE_RANGE),
    ],
)
def test_mapping_rejects_invalid_episode_ranges(
    season: int,
    episode_start: int,
    episode_end: int,
    expected_code: ErrorCode,
) -> None:
    payload = valid_payload()
    payload["videos"] = [
        {
            "video_id": "video:1",
            "season": season,
            "episode_start": episode_start,
            "episode_end": episode_end,
        }
    ]
    payload["subtitles"] = []

    with pytest.raises(DomainError) as raised:
        MappingDraft.from_dict(
            payload,
            candidates=candidate_snapshot(),
            catalog=episode_catalog(),
        )

    assert raised.value.code is expected_code


def test_mapping_rejects_overlapping_episode_ranges() -> None:
    payload = valid_payload()
    payload["videos"] = [
        {
            "video_id": "video:1",
            "season": 1,
            "episode_start": 1,
            "episode_end": 2,
        },
        {
            "video_id": "video:2",
            "season": 1,
            "episode_start": 2,
            "episode_end": 3,
        },
    ]
    payload["subtitles"] = []

    with pytest.raises(DomainError) as raised:
        MappingDraft.from_dict(
            payload,
            candidates=candidate_snapshot(),
            catalog=episode_catalog(),
        )

    assert raised.value.code is ErrorCode.EPISODE_RANGE_OVERLAP
    assert raised.value.context == {
        "season": 1,
        "episode": 2,
        "video_ids": ("video:1", "video:2"),
    }


def test_mapping_rejects_duplicate_video_mapping() -> None:
    payload = valid_payload()
    payload["videos"] = [
        payload["videos"][0],  # type: ignore[index]
        payload["videos"][0],  # type: ignore[index]
    ]
    payload["subtitles"] = []

    with pytest.raises(DomainError) as raised:
        MappingDraft.from_dict(
            payload,
            candidates=candidate_snapshot(),
            catalog=episode_catalog(),
        )

    assert raised.value.code is ErrorCode.DUPLICATE_VIDEO_MAPPING
    assert raised.value.context == {"video_id": "video:1"}


def test_subtitle_must_reference_a_mapped_video() -> None:
    payload = valid_payload()
    payload["videos"] = [payload["videos"][0]]  # type: ignore[index]

    with pytest.raises(DomainError) as raised:
        MappingDraft.from_dict(
            payload,
            candidates=candidate_snapshot(),
            catalog=episode_catalog(),
        )

    assert raised.value.code is ErrorCode.SUBTITLE_VIDEO_NOT_MAPPED
    assert raised.value.context == {
        "subtitle_id": "subtitle:1",
        "video_id": "video:2",
    }


def test_mapping_rejects_duplicate_subtitle_mapping() -> None:
    payload = valid_payload()
    payload["subtitles"] = [
        payload["subtitles"][0],  # type: ignore[index]
        payload["subtitles"][0],  # type: ignore[index]
    ]

    with pytest.raises(DomainError) as raised:
        MappingDraft.from_dict(
            payload,
            candidates=candidate_snapshot(),
            catalog=episode_catalog(),
        )

    assert raised.value.code is ErrorCode.DUPLICATE_SUBTITLE_MAPPING
    assert raised.value.context == {"subtitle_id": "subtitle:1"}
