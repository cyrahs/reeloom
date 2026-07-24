from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeAlias

_SCHEMA_VERSION = "scripted-transcript-v1"
_MAX_TRANSCRIPT_BYTES = 1024 * 1024
_MAX_STEPS = 128
_MAX_TEXT_BYTES = 64 * 1024
_MAX_ARGUMENT_BYTES = 64 * 1024
_MAX_IDENTIFIER_BYTES = 160


def _bounded(value: object, *, limit: int, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value.encode("utf-8")) > limit
    ):
        raise ValueError("invalid transcript text")
    return value


@dataclass(frozen=True, slots=True)
class ToolCallStep:
    name: str
    arguments: Mapping[str, object] | str
    call_id: str
    expect_input_contains: str | None = None

    def __post_init__(self) -> None:
        _bounded(self.name, limit=_MAX_IDENTIFIER_BYTES)
        _bounded(self.call_id, limit=_MAX_IDENTIFIER_BYTES)
        if self.expect_input_contains is not None:
            _bounded(
                self.expect_input_contains,
                limit=_MAX_TEXT_BYTES,
                allow_empty=True,
            )
        _arguments_json(self.arguments)


@dataclass(frozen=True, slots=True)
class FinalStep:
    text: str
    expect_input_contains: str | None = None

    def __post_init__(self) -> None:
        _bounded(self.text, limit=_MAX_TEXT_BYTES, allow_empty=True)
        if self.expect_input_contains is not None:
            _bounded(
                self.expect_input_contains,
                limit=_MAX_TEXT_BYTES,
                allow_empty=True,
            )


ScriptStep: TypeAlias = ToolCallStep | FinalStep


def _arguments_json(value: Mapping[str, object] | str) -> str:
    try:
        encoded = (
            value
            if isinstance(value, str)
            else json.dumps(
                value,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    except (TypeError, ValueError):
        raise ValueError("invalid tool arguments") from None
    return _bounded(
        encoded,
        limit=_MAX_ARGUMENT_BYTES,
        allow_empty=True,
    )


def _check_fields(
    value: object,
    fields: frozenset[str],
) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != fields:
        raise ValueError("invalid transcript schema")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _bounded(value, limit=_MAX_TEXT_BYTES, allow_empty=True)


@dataclass(frozen=True, slots=True, init=False)
class ScriptedTranscript:
    schema_version: str
    steps: tuple[ScriptStep, ...]
    transcript_hash: str

    @classmethod
    def create(cls, steps: tuple[ScriptStep, ...]) -> ScriptedTranscript:
        if (
            not isinstance(steps, tuple)
            or not 0 < len(steps) <= _MAX_STEPS
            or any(not isinstance(step, (ToolCallStep, FinalStep)) for step in steps)
        ):
            raise ValueError("invalid transcript steps")
        frozen_steps = tuple(
            ToolCallStep(
                name=step.name,
                arguments=_arguments_json(step.arguments),
                call_id=step.call_id,
                expect_input_contains=step.expect_input_contains,
            )
            if isinstance(step, ToolCallStep)
            else step
            for step in steps
        )
        transcript = object.__new__(cls)
        object.__setattr__(transcript, "schema_version", _SCHEMA_VERSION)
        object.__setattr__(transcript, "steps", frozen_steps)
        object.__setattr__(
            transcript,
            "transcript_hash",
            "sha256:"
            + hashlib.sha256(transcript.canonical_bytes()).hexdigest(),
        )
        return transcript

    @classmethod
    def from_canonical_bytes(
        cls,
        content: bytes,
    ) -> ScriptedTranscript:
        if (
            not isinstance(content, bytes)
            or not 0 < len(content) <= _MAX_TRANSCRIPT_BYTES
        ):
            raise ValueError("invalid transcript")
        try:
            payload = _check_fields(
                json.loads(content),
                frozenset({"schema_version", "steps"}),
            )
            if payload["schema_version"] != _SCHEMA_VERSION:
                raise ValueError
            raw_steps = payload["steps"]
            if not isinstance(raw_steps, list):
                raise ValueError
            steps: list[ScriptStep] = []
            for item in raw_steps:
                if not isinstance(item, dict):
                    raise ValueError
                step_type = item.get("type")
                if step_type == "tool_call":
                    step = _check_fields(
                        item,
                        frozenset(
                            {
                                "arguments_json",
                                "call_id",
                                "expect_input_contains",
                                "name",
                                "type",
                            }
                        ),
                    )
                    steps.append(
                        ToolCallStep(
                            name=_bounded(
                                step["name"],
                                limit=_MAX_IDENTIFIER_BYTES,
                            ),
                            arguments=_bounded(
                                step["arguments_json"],
                                limit=_MAX_ARGUMENT_BYTES,
                                allow_empty=True,
                            ),
                            call_id=_bounded(
                                step["call_id"],
                                limit=_MAX_IDENTIFIER_BYTES,
                            ),
                            expect_input_contains=_optional_text(
                                step["expect_input_contains"]
                            ),
                        )
                    )
                elif step_type == "final":
                    step = _check_fields(
                        item,
                        frozenset(
                            {"expect_input_contains", "text", "type"}
                        ),
                    )
                    steps.append(
                        FinalStep(
                            text=_bounded(
                                step["text"],
                                limit=_MAX_TEXT_BYTES,
                                allow_empty=True,
                            ),
                            expect_input_contains=_optional_text(
                                step["expect_input_contains"]
                            ),
                        )
                    )
                else:
                    raise ValueError
            transcript = cls.create(tuple(steps))
            if transcript.canonical_bytes() != content:
                raise ValueError
            return transcript
        except (
            json.JSONDecodeError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
        ):
            raise ValueError("invalid transcript") from None

    def canonical_bytes(self) -> bytes:
        payload = {
            "schema_version": self.schema_version,
            "steps": [
                (
                    {
                        "arguments_json": _arguments_json(step.arguments),
                        "call_id": step.call_id,
                        "expect_input_contains": step.expect_input_contains,
                        "name": step.name,
                        "type": "tool_call",
                    }
                    if isinstance(step, ToolCallStep)
                    else {
                        "expect_input_contains": step.expect_input_contains,
                        "text": step.text,
                        "type": "final",
                    }
                )
                for step in self.steps
            ],
        }
        content = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        if len(content) > _MAX_TRANSCRIPT_BYTES:
            raise ValueError("transcript too large")
        return content
