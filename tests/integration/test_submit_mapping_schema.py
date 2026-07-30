from __future__ import annotations

import hashlib
import json

import pytest
from agents.models.openai_responses import Converter

from reeloom.agents.organizer import (
    _episode_submit_payload,
    _movie_submit_payload,
    _submit_mapping_tool,
    _submit_movie_mapping_tool,
)
from reeloom.kernel.plan_review import (
    MAX_REVIEW_BYTES,
    PlanReviewReason,
)

_REASONS = {reason.value for reason in PlanReviewReason}


def _empty_nodes(value: object) -> list[dict[object, object]]:
    if isinstance(value, dict):
        return (
            [value] if not value else []
        ) + [
            item
            for child in value.values()
            for item in _empty_nodes(child)
        ]
    if isinstance(value, list):
        return [
            item
            for child in value
            for item in _empty_nodes(child)
        ]
    return []


@pytest.mark.parametrize(
    ("tool", "expected_hash"),
    (
        (
            _submit_mapping_tool,
            "c0ab15e0b3989bfb6051f290b15db7cc"
            "1a94a656bcf621320127b63133a35248",
        ),
        (
            _submit_movie_mapping_tool,
            "3e32c623033d09a5a4b0a3bb2c59bd832"
            "7e6775eecc3f7cc7259d470d04c1873",
        ),
    ),
)
def test_submit_mapping_provider_schema_is_strict_and_stable(
    tool: object,
    expected_hash: str,
) -> None:
    converted = Converter.convert_tools([tool], []).tools[0]
    schema = converted["parameters"]

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert not _empty_nodes(schema)
    review = schema["$defs"]["_PlanReviewInput"]
    explanation = schema["$defs"]["_UnmappedExplanationInput"]
    assert review["type"] == "object"
    assert review["additionalProperties"] is False
    assert explanation["type"] == "object"
    assert explanation["additionalProperties"] is False
    reason = schema["$defs"]["PlanReviewReason"]
    assert reason["type"] == "string"
    assert set(reason["enum"]) == _REASONS
    canonical = json.dumps(
        converted,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert hashlib.sha256(canonical).hexdigest() == expected_hash


@pytest.mark.parametrize(
    "review",
    (
        None,
        7,
        {"unexpected": True},
        {
            "summary": "x" * 4_097,
            "unmapped_explanations": [],
        },
    ),
)
def test_submit_keeps_review_validation_soft(
    review: object,
) -> None:
    episode_payload: dict[str, object] = {
        "videos": [
            {
                "video_id": "video:1",
                "season": 1,
                "episode_start": 1,
                "episode_end": 1,
            }
        ],
        "subtitles": [],
        "review": review,
    }

    episode = _episode_submit_payload(
        json.dumps(episode_payload),
        candidate_count=1,
    )
    movie = _movie_submit_payload(
        json.dumps(
            {
                "video_id": "video:1",
                "subtitle_ids": [],
                "review": review,
            }
        ),
        candidate_count=1,
    )

    assert episode is not None
    assert episode[1] == review
    assert movie is not None
    assert movie[1] == review


def test_submit_parsers_bound_deep_review_json() -> None:
    review = "[" * 10_000 + "]" * 10_000
    episode = (
        '{"videos":[{"video_id":"video:1","season":1,'
        '"episode_start":1,"episode_end":1}],"subtitles":[],'
        f'"review":{review}}}'
    )
    movie = (
        '{"video_id":"video:1","subtitle_ids":[],'
        f'"review":{review}}}'
    )
    assert len(episode.encode()) < MAX_REVIEW_BYTES
    assert len(movie.encode()) < MAX_REVIEW_BYTES

    assert _episode_submit_payload(episode, candidate_count=1) is None
    assert _movie_submit_payload(movie, candidate_count=1) is None


def test_submit_parser_accepts_omitted_review_but_rejects_bad_mapping() -> None:
    assert (
        _movie_submit_payload(
            '{"video_id":"video:1","subtitle_ids":[]}',
            candidate_count=1,
        )
        is not None
    )
    assert (
        _movie_submit_payload(
            '{"video_id":"video:1","subtitle_ids":[],'
            '"review":null,"video_id":"video:2"}',
            candidate_count=2,
        )
        is None
    )
    assert (
        _episode_submit_payload(
            '{"videos":[{"video_id":"video:1","season":"1",'
            '"episode_start":1,"episode_end":1}],"subtitles":[],'
            '"review":null}',
            candidate_count=1,
        )
        is None
    )
