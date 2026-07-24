from __future__ import annotations

import json

import pytest

from reeloom.agents.scripted_model import ScriptedModel
from reeloom.agents.transcript import (
    FinalStep,
    ScriptedTranscript,
    ToolCallStep,
)


def _transcript(arguments: dict[str, object] | None = None) -> ScriptedTranscript:
    return ScriptedTranscript.create(
        (
            ToolCallStep(
                name="list_candidates",
                arguments=arguments or {"kind": "video", "limit": 10},
                call_id="call-1",
            ),
            FinalStep(
                text="done",
                expect_input_contains="video:1",
            ),
        )
    )


def test_scripted_transcript_is_canonical_and_immutable() -> None:
    arguments: dict[str, object] = {"kind": "video", "limit": 10}
    transcript = _transcript(arguments)
    original = transcript.canonical_bytes()
    arguments["limit"] = 999

    restored = ScriptedTranscript.from_canonical_bytes(original)

    assert restored == transcript
    assert restored.canonical_bytes() == original
    assert restored.transcript_hash == transcript.transcript_hash
    assert ScriptedModel(restored).consumed_steps == 0


def test_scripted_transcript_preserves_malformed_arguments() -> None:
    transcript = ScriptedTranscript.create(
        (
            ToolCallStep(
                name="submit_mapping",
                arguments="{malformed",
                call_id="call-1",
            ),
        )
    )

    restored = ScriptedTranscript.from_canonical_bytes(
        transcript.canonical_bytes()
    )

    assert restored.steps == transcript.steps


def test_scripted_transcript_rejects_extra_and_noncanonical_data() -> None:
    content = _transcript().canonical_bytes()
    payload = json.loads(content)
    payload["unexpected"] = True
    extra = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")

    for invalid in (content + b"\n", extra):
        with pytest.raises(ValueError):
            ScriptedTranscript.from_canonical_bytes(invalid)
