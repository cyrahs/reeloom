from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.forward_execution import (
    ExecutionItemOutcome,
    ExecutionOperation,
    ExecutionOperationStatus,
    ForwardMoveDecision,
    PathObservationState,
    RenamePlanV2,
    compile_plan_draft_v2,
    decide_forward_move,
    reduce_execution_status,
)
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.naming import SeriesIdentity, SubtitleVariant
from reeloom.kernel.semantic_identity import (
    SemanticCandidateSnapshot,
    SemanticRootBinding,
    SemanticSourceIdentity,
)
from reeloom.kernel.tmdb import TmdbWorkType


_EXPECTED_MATRIX = (
    # destination: absent, matching, mismatched, unsafe, unavailable
    (
        ForwardMoveDecision.STALE,
        ForwardMoveDecision.SATISFIED,
        ForwardMoveDecision.COLLISION,
        ForwardMoveDecision.UNSAFE,
        ForwardMoveDecision.UNAVAILABLE,
    ),
    (
        ForwardMoveDecision.MOVE,
        ForwardMoveDecision.COLLISION,
        ForwardMoveDecision.COLLISION,
        ForwardMoveDecision.UNSAFE,
        ForwardMoveDecision.UNAVAILABLE,
    ),
    (
        ForwardMoveDecision.STALE,
        ForwardMoveDecision.STALE,
        ForwardMoveDecision.COLLISION,
        ForwardMoveDecision.UNSAFE,
        ForwardMoveDecision.UNAVAILABLE,
    ),
    (
        ForwardMoveDecision.UNSAFE,
        ForwardMoveDecision.UNSAFE,
        ForwardMoveDecision.UNSAFE,
        ForwardMoveDecision.UNSAFE,
        ForwardMoveDecision.UNAVAILABLE,
    ),
    (
        ForwardMoveDecision.UNAVAILABLE,
        ForwardMoveDecision.UNAVAILABLE,
        ForwardMoveDecision.UNAVAILABLE,
        ForwardMoveDecision.UNAVAILABLE,
        ForwardMoveDecision.UNAVAILABLE,
    ),
)


@pytest.mark.parametrize(
    "source,destination",
    tuple(itertools.product(PathObservationState, repeat=2)),
)
def test_forward_move_truth_table_is_exhaustive(
    source: PathObservationState,
    destination: PathObservationState,
) -> None:
    states = tuple(PathObservationState)
    expected = _EXPECTED_MATRIX[states.index(source)][states.index(destination)]

    assert decide_forward_move(source, destination) is expected


def test_truth_table_rejects_untyped_observations() -> None:
    with pytest.raises(DomainError) as raised:
        decide_forward_move("matching", PathObservationState.ABSENT)  # type: ignore[arg-type]
    assert raised.value.code is ErrorCode.INVALID_FIELD_TYPE


def test_operation_reducer_always_returns_a_terminal_status() -> None:
    for length in range(1, 4):
        for outcomes in itertools.product(ExecutionItemOutcome, repeat=length):
            assert reduce_execution_status(outcomes).terminal

    assert reduce_execution_status(
        (ExecutionItemOutcome.SATISFIED,) * 2
    ) is ExecutionOperationStatus.COMPLETED
    assert reduce_execution_status(
        (ExecutionItemOutcome.SATISFIED, ExecutionItemOutcome.COLLISION)
    ) is ExecutionOperationStatus.PARTIAL
    with pytest.raises(DomainError):
        reduce_execution_status(())


def test_operation_lifecycle_reconciles_without_a_new_operation() -> None:
    operation = ExecutionOperation.authorized(
        operation_id="operation:1",
        run_id="run:1",
        plan_hash="sha256:" + "a" * 64,
    )

    first_attempt = operation.begin_or_reconcile()
    second_attempt = first_attempt.begin_or_reconcile()
    settled = second_attempt.settle(
        (ExecutionItemOutcome.SATISFIED, ExecutionItemOutcome.STALE)
    )

    assert operation.status is ExecutionOperationStatus.AUTHORIZED
    assert first_attempt.status is ExecutionOperationStatus.RUNNING
    assert second_attempt.operation_id == first_attempt.operation_id
    assert second_attempt.attempt_count == 2
    assert settled.status is ExecutionOperationStatus.PARTIAL
    assert settled.terminal
    with pytest.raises(DomainError):
        settled.begin_or_reconcile()
    with pytest.raises(FrozenInstanceError):
        operation.status = ExecutionOperationStatus.RUNNING  # type: ignore[misc]


def test_unstarted_or_running_operation_can_be_superseded() -> None:
    operation = ExecutionOperation.authorized(
        operation_id="operation:legacy",
        run_id="run:legacy",
        plan_hash="sha256:" + "b" * 64,
    )

    assert operation.supersede().status is ExecutionOperationStatus.SUPERSEDED
    assert (
        operation.begin_or_reconcile().supersede().status
        is ExecutionOperationStatus.SUPERSEDED
    )


def _snapshot(
    *,
    video_size: int = 1_024,
    video_path: str = "release/episode.mkv",
) -> SemanticCandidateSnapshot:
    return SemanticCandidateSnapshot.create(
        (
            SemanticSourceIdentity(
                candidate_id=CandidateId(CandidateKind.VIDEO, 1),
                kind=CandidateKind.VIDEO,
                relative_path=PurePosixPath(video_path),
                size_bytes=video_size,
            ),
            SemanticSourceIdentity(
                candidate_id=CandidateId(CandidateKind.SUBTITLE, 1),
                kind=CandidateKind.SUBTITLE,
                relative_path=PurePosixPath("release/episode.ass"),
                size_bytes=128,
                sha256="a" * 64,
            ),
            SemanticSourceIdentity(
                candidate_id=CandidateId(CandidateKind.VIDEO, 2),
                kind=CandidateKind.VIDEO,
                relative_path=PurePosixPath("release/unmapped.mkv"),
                size_bytes=2_048,
            ),
        )
    )


def _plan(*, video_size: int = 1_024) -> RenamePlanV2:
    snapshot = _snapshot(video_size=video_size)
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
            "subtitles": [
                {"subtitle_id": "subtitle:1", "video_id": "video:1"}
            ],
        },
        candidates=snapshot.candidates,
        catalog=EpisodeCatalog.from_counts({1: 12}),
    )
    series = SeriesIdentity(
        title_zh_cn="Series",
        year=2026,
        tmdb_id=14,
    )
    variants = (
        (CandidateId(CandidateKind.SUBTITLE, 1), SubtitleVariant.CHS),
    )
    draft = compile_plan_draft_v2(
        series=series,
        mapping=mapping,
        candidates=snapshot,
        subtitle_variants=variants,
    )
    return RenamePlanV2.create(
        run_id="run-m14",
        config_revision=7,
        watch_id="watch-anime",
        work_type=TmdbWorkType.ANIME,
        created_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        source_root=SemanticRootBinding(PurePosixPath("/media/incoming")),
        output_root=SemanticRootBinding(PurePosixPath("/media/library")),
        candidate_snapshot=snapshot,
        subtitle_variants=variants,
        draft=draft,
    )


def test_v2_plan_is_canonical_strict_and_free_of_stat_identity() -> None:
    plan = _plan()
    payload = json.loads(plan.canonical_bytes())

    assert plan.verify_hash()
    assert RenamePlanV2.from_canonical_bytes(
        plan.canonical_bytes(), plan_hash=plan.plan_hash
    ) == plan
    assert payload["schema_version"] == "2"
    assert payload["series"] == {
        "title_zh_cn": "Series",
        "tmdb_id": 14,
        "year": 2026,
    }
    assert payload["moves"] == [
        {
            "destination": "Series (2026) {tmdb-14}/S01/Series S01E01.mkv",
            "episode_end": 1,
            "episode_start": 1,
            "season": 1,
            "source_id": "video:1",
            "video_id": "video:1",
        },
        {
            "destination": "Series (2026) {tmdb-14}/S01/Series S01E01.chs.ass",
            "episode_end": 1,
            "episode_start": 1,
            "season": 1,
            "source_id": "subtitle:1",
            "video_id": "video:1",
        },
    ]
    assert payload["roots"] == {
        "output": {"path": "/media/library"},
        "source": {"path": "/media/incoming"},
    }
    encoded = plan.canonical_bytes()
    for forbidden in (
        b'"device"',
        b'"inode"',
        b'"mtime"',
        b'"mtime_ns"',
        b'"ctime"',
        b'"ctime_ns"',
        b'"parent_plan_hash"',
    ):
        assert forbidden not in encoded


def test_only_semantic_source_change_changes_v2_plan_hash() -> None:
    original = _plan(video_size=1_024)
    changed = _plan(video_size=1_025)

    assert original.candidate_snapshot.snapshot_id != changed.candidate_snapshot.snapshot_id
    assert original.plan_hash != changed.plan_hash


def test_v2_plan_decoder_rejects_tamper_extra_and_noncanonical_bytes() -> None:
    plan = _plan()
    payload = json.loads(plan.canonical_bytes())
    payload["sources"][0]["size_bytes"] += 1
    tampered = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()
    tampered_hash = "sha256:" + hashlib.sha256(tampered).hexdigest()
    with pytest.raises(DomainError):
        RenamePlanV2.from_canonical_bytes(tampered, plan_hash=tampered_hash)

    payload = json.loads(plan.canonical_bytes())
    payload["moves"][0]["destination"] = "Agent/selected/path.mkv"
    tampered_destination = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()
    tampered_destination_hash = (
        "sha256:" + hashlib.sha256(tampered_destination).hexdigest()
    )
    with pytest.raises(DomainError) as raised:
        RenamePlanV2.from_canonical_bytes(
            tampered_destination,
            plan_hash=tampered_destination_hash,
        )
    assert raised.value.code is ErrorCode.PLAN_MAPPING_MISMATCH

    payload = json.loads(plan.canonical_bytes())
    payload["inode"] = 99
    extra = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()
    extra_hash = "sha256:" + hashlib.sha256(extra).hexdigest()
    with pytest.raises(DomainError) as raised:
        RenamePlanV2.from_canonical_bytes(extra, plan_hash=extra_hash)
    assert raised.value.code is ErrorCode.EXTRA_KEYS

    noncanonical = plan.canonical_bytes() + b"\n"
    noncanonical_hash = "sha256:" + hashlib.sha256(noncanonical).hexdigest()
    with pytest.raises(DomainError):
        RenamePlanV2.from_canonical_bytes(
            noncanonical, plan_hash=noncanonical_hash
        )

    duplicate_key = plan.canonical_bytes().replace(
        b'{"candidate_snapshot_id":',
        b'{"run_id":"duplicate","candidate_snapshot_id":',
        1,
    )
    duplicate_key_hash = "sha256:" + hashlib.sha256(duplicate_key).hexdigest()
    with pytest.raises(DomainError):
        RenamePlanV2.from_canonical_bytes(
            duplicate_key, plan_hash=duplicate_key_hash
        )


def test_v2_plan_rejects_draft_from_a_different_semantic_snapshot() -> None:
    snapshot = _snapshot()
    changed_snapshot = _snapshot(video_path="release/episode.mp4")
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
            "subtitles": [
                {"subtitle_id": "subtitle:1", "video_id": "video:1"}
            ],
        },
        candidates=snapshot.candidates,
        catalog=EpisodeCatalog.from_counts({1: 12}),
    )
    variants = (
        (CandidateId(CandidateKind.SUBTITLE, 1), SubtitleVariant.CHS),
    )
    draft = compile_plan_draft_v2(
        series=SeriesIdentity("Series", 2026, 14),
        mapping=mapping,
        candidates=snapshot,
        subtitle_variants=variants,
    )

    with pytest.raises(DomainError) as raised:
        RenamePlanV2.create(
            run_id="run-m14",
            config_revision=7,
            watch_id="watch-anime",
            work_type=TmdbWorkType.ANIME,
            created_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
            source_root=SemanticRootBinding(PurePosixPath("/media/incoming")),
            output_root=SemanticRootBinding(PurePosixPath("/media/library")),
            candidate_snapshot=changed_snapshot,
            subtitle_variants=variants,
            draft=draft,
        )
    assert raised.value.code is ErrorCode.PLAN_MAPPING_MISMATCH
