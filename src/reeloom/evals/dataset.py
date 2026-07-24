from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from reeloom.adapters._immutable_file import (
    ImmutableFileError,
    open_root,
    read_at,
)
from reeloom.agents.transcript import ScriptedTranscript
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError
from reeloom.kernel.mapping import EpisodeSpan
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import (
    AuthorizedRoot,
    is_forbidden_env_name,
)
from reeloom.runtime.state import Phase, RunStatus, StopReason

_SCHEMA_VERSION = "reeloom-eval-dataset-v1"
_MAX_DATASET_BYTES = 4 * 1024 * 1024
_MAX_TASKS = 128
_MAX_TEXT_BYTES = 64 * 1024
_MAX_ID_BYTES = 160
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,159}$")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate eval dataset key")
        payload[key] = value
    return payload


def _fields(value: object, names: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != names:
        raise ValueError("invalid eval dataset schema")
    return value


def _string(value: object, *, max_bytes: int, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value.encode("utf-8")) > max_bytes
    ):
        raise ValueError("invalid eval dataset string")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid eval expectation")
    return value


def _identifier(value: object) -> str:
    result = _string(value, max_bytes=_MAX_ID_BYTES)
    if _IDENTIFIER.fullmatch(result) is None:
        raise ValueError("invalid eval identifier")
    return result


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("invalid eval expectation")
    return value


@dataclass(frozen=True, slots=True)
class EvalExpectation:
    phase: Phase
    status: RunStatus
    stop_reason: StopReason
    mapping_success: bool
    clarification_required: bool
    selected_tmdb_id: int
    videos: tuple[EvalVideoMapping, ...]
    subtitles: tuple[EvalSubtitleMapping, ...]
    unmapped_candidate_ids: tuple[CandidateId, ...]
    scripted_process: EvalProcessExpectation


@dataclass(frozen=True, slots=True)
class EvalVideoMapping:
    video_id: CandidateId
    span: EpisodeSpan


@dataclass(frozen=True, slots=True)
class EvalSubtitleMapping:
    subtitle_id: CandidateId
    video_id: CandidateId


class EvalRejectionKind(StrEnum):
    MAPPING = "mapping"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class EvalRejection:
    kind: EvalRejectionKind
    call_id: str
    code: str


@dataclass(frozen=True, slots=True)
class EvalProcessExpectation:
    tool_calls: int
    rejections: tuple[EvalRejection, ...]


@dataclass(frozen=True, slots=True)
class EvalTask:
    task_id: str
    scenario: str
    prompt: str
    work_type: TmdbWorkType
    transcript: ScriptedTranscript
    expectation: EvalExpectation


@dataclass(frozen=True, slots=True)
class EvalDataset:
    schema_version: str
    tasks: tuple[EvalTask, ...]
    dataset_hash: str

    @classmethod
    def from_bytes(cls, content: bytes) -> EvalDataset:
        if (
            not isinstance(content, bytes)
            or not 0 < len(content) <= _MAX_DATASET_BYTES
        ):
            raise ValueError("invalid eval dataset")
        try:
            payload = _fields(
                json.loads(
                    content,
                    object_pairs_hook=_reject_duplicate_keys,
                ),
                frozenset({"schema_version", "tasks"}),
            )
            if payload["schema_version"] != _SCHEMA_VERSION:
                raise ValueError
            raw_tasks = payload["tasks"]
            if (
                not isinstance(raw_tasks, list)
                or not 0 < len(raw_tasks) <= _MAX_TASKS
            ):
                raise ValueError
            tasks = tuple(_task(item) for item in raw_tasks)
            if len({task.task_id for task in tasks}) != len(tasks):
                raise ValueError
            canonical = json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            return cls(
                schema_version=_SCHEMA_VERSION,
                tasks=tasks,
                dataset_hash=(
                    "sha256:" + hashlib.sha256(canonical).hexdigest()
                ),
            )
        except (
            json.JSONDecodeError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
        ):
            raise ValueError("invalid eval dataset") from None

    @classmethod
    def load(cls, path: Path) -> EvalDataset:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or not path.name
            or any(
                is_forbidden_env_name(part)
                for part in path.parts
                if part != path.anchor
            )
        ):
            raise ValueError("invalid eval dataset path")
        root_fd: int | None = None
        try:
            root = AuthorizedRoot.create(path.parent)
            root_fd = open_root(root)
            content = read_at(
                root_fd,
                path.name,
                limit=_MAX_DATASET_BYTES,
            )
        except (DomainError, ImmutableFileError, OSError, ValueError):
            raise ValueError("cannot read eval dataset") from None
        finally:
            if root_fd is not None:
                os.close(root_fd)
        return cls.from_bytes(content)


def _task(value: object) -> EvalTask:
    payload = _fields(
        value,
        frozenset(
            {
                "expectation",
                "prompt",
                "scenario",
                "task_id",
                "transcript",
                "work_type",
            }
        ),
    )
    transcript_payload = payload["transcript"]
    transcript_bytes = json.dumps(
        transcript_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    expectation = _expectation(payload["expectation"])
    try:
        work_type = TmdbWorkType(
            _string(payload["work_type"], max_bytes=_MAX_ID_BYTES)
        )
    except ValueError:
        raise ValueError("invalid eval work type") from None
    return EvalTask(
        task_id=_identifier(payload["task_id"]),
        scenario=_identifier(payload["scenario"]),
        prompt=_string(
            payload["prompt"],
            max_bytes=_MAX_TEXT_BYTES,
            allow_empty=True,
        ),
        work_type=work_type,
        transcript=ScriptedTranscript.from_canonical_bytes(
            transcript_bytes
        ),
        expectation=expectation,
    )


def _expectation(value: object) -> EvalExpectation:
    payload = _fields(
        value,
        frozenset(
            {
                "clarification_required",
                "mapping_success",
                "phase",
                "scripted_process",
                "selected_tmdb_id",
                "status",
                "stop_reason",
                "subtitles",
                "unmapped_candidate_ids",
                "videos",
            }
        ),
    )
    try:
        return EvalExpectation(
            phase=Phase(_string(payload["phase"], max_bytes=_MAX_ID_BYTES)),
            status=RunStatus(
                _string(payload["status"], max_bytes=_MAX_ID_BYTES)
            ),
            stop_reason=StopReason(
                _string(payload["stop_reason"], max_bytes=_MAX_ID_BYTES)
            ),
            mapping_success=_boolean(payload["mapping_success"]),
            clarification_required=_boolean(
                payload["clarification_required"]
            ),
            selected_tmdb_id=_positive_integer(
                payload["selected_tmdb_id"]
            ),
            videos=_videos(payload["videos"]),
            subtitles=_subtitles(payload["subtitles"]),
            unmapped_candidate_ids=_candidate_ids(
                payload["unmapped_candidate_ids"]
            ),
            scripted_process=_process_expectation(
                payload["scripted_process"]
            ),
        )
    except (DomainError, ValueError):
        raise ValueError("invalid eval expectation") from None


def _positive_integer(value: object) -> int:
    result = _integer(value)
    if result < 1:
        raise ValueError("invalid eval expectation")
    return result


def _list(value: object) -> list[object]:
    if not isinstance(value, list) or len(value) > 10_000:
        raise ValueError("invalid eval expectation")
    return value


def _candidate_id(
    value: object,
    *,
    kind: CandidateKind | None = None,
) -> CandidateId:
    candidate_id = CandidateId.parse(value)
    if kind is not None and candidate_id.kind is not kind:
        raise ValueError("invalid eval candidate kind")
    return candidate_id


def _candidate_ids(value: object) -> tuple[CandidateId, ...]:
    result = tuple(_candidate_id(item) for item in _list(value))
    if len(set(result)) != len(result):
        raise ValueError("duplicate eval candidate")
    return result


def _videos(value: object) -> tuple[EvalVideoMapping, ...]:
    result: list[EvalVideoMapping] = []
    for item in _list(value):
        payload = _fields(
            item,
            frozenset(
                {
                    "episode_end",
                    "episode_start",
                    "season",
                    "video_id",
                }
            ),
        )
        result.append(
            EvalVideoMapping(
                video_id=_candidate_id(
                    payload["video_id"],
                    kind=CandidateKind.VIDEO,
                ),
                span=EpisodeSpan(
                    season=_integer(payload["season"]),
                    episode_start=_positive_integer(
                        payload["episode_start"]
                    ),
                    episode_end=_positive_integer(
                        payload["episode_end"]
                    ),
                ),
            )
        )
    if len({item.video_id for item in result}) != len(result):
        raise ValueError("duplicate eval video")
    return tuple(result)


def _subtitles(value: object) -> tuple[EvalSubtitleMapping, ...]:
    result: list[EvalSubtitleMapping] = []
    for item in _list(value):
        payload = _fields(
            item,
            frozenset({"subtitle_id", "video_id"}),
        )
        result.append(
            EvalSubtitleMapping(
                subtitle_id=_candidate_id(
                    payload["subtitle_id"],
                    kind=CandidateKind.SUBTITLE,
                ),
                video_id=_candidate_id(
                    payload["video_id"],
                    kind=CandidateKind.VIDEO,
                ),
            )
        )
    if len({item.subtitle_id for item in result}) != len(result):
        raise ValueError("duplicate eval subtitle")
    return tuple(result)


def _process_expectation(value: object) -> EvalProcessExpectation:
    payload = _fields(
        value,
        frozenset({"rejections", "tool_calls"}),
    )
    rejections: list[EvalRejection] = []
    for item in _list(payload["rejections"]):
        rejection = _fields(
            item,
            frozenset({"call_id", "code", "kind"}),
        )
        rejections.append(
            EvalRejection(
                kind=EvalRejectionKind(
                    _string(
                        rejection["kind"],
                        max_bytes=_MAX_ID_BYTES,
                    )
                ),
                call_id=_identifier(rejection["call_id"]),
                code=_identifier(rejection["code"]),
            )
        )
    if len(set(rejections)) != len(rejections):
        raise ValueError("duplicate eval rejection")
    return EvalProcessExpectation(
        tool_calls=_integer(payload["tool_calls"]),
        rejections=tuple(rejections),
    )
