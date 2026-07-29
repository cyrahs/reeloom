from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import PurePosixPath
from typing import cast

import pytest
from psycopg_pool import ConnectionPool

from reeloom.kernel.amendment import (
    CompletedLayout,
    CompletedLayoutFile,
    DesiredLayoutMove,
    compile_amendment,
)
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.movie import (
    MovieMappingDraft,
    compile_movie_plan_draft,
)
from reeloom.kernel.movie_plan import MovieRenamePlan
from reeloom.kernel.naming import MovieIdentity
from reeloom.kernel.rename_plan import RootBinding
from reeloom.kernel.scanner import ScannedFile, build_candidate_snapshot
from reeloom.kernel.plan_review import (
    PlanReviewReason,
    PlanReviewStatus,
    PlanReviewVerification,
)
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.queries import PostgresQueries


class _Cursor:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...]:
        return self._row


class _Connection:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(
        self,
        query: object,
        parameters: object,
    ) -> _Cursor:
        del query, parameters
        return _Cursor(self._row)


class _Pool:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row

    def connection(self) -> _Connection:
        return _Connection(self._row)


class _Plans:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def load(self, plan_hash: str) -> bytes:
        del plan_hash
        return self._content


def _amendment() -> tuple[str, bytes]:
    candidate_id = CandidateId(CandidateKind.VIDEO, 1)
    layout = CompletedLayout(
        run_id="run-1",
        original_plan_hash="sha256:" + "a" * 64,
        transaction_id="txn-v1-" + "b" * 64,
        root=RootBinding(PurePosixPath("/archive"), 1, 2),
        files=(
            CompletedLayoutFile(
                candidate_id=candidate_id,
                kind=CandidateKind.VIDEO,
                relative_path=PurePosixPath(
                    "Series/S01/Series - S01E01.mkv"
                ),
                size_bytes=5,
                device=1,
                inode=10,
                mtime_ns=20,
                ctime_ns=30,
                sample_digest=None,
            ),
        ),
    )
    plan = compile_amendment(
        layout=layout,
        desired=(
            DesiredLayoutMove(
                source_id=candidate_id,
                video_id=candidate_id,
                destination=PurePosixPath(
                    "Series/S00/Series - S00E01.mkv"
                ),
                season=0,
                episode_start=1,
                episode_end=1,
            ),
        ),
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    assert plan is not None
    return plan.plan_hash, plan.canonical_bytes()


def test_preview_rejects_amendment_mislabeled_as_initial() -> None:
    plan_hash, content = _amendment()
    queries = PostgresQueries(
        cast(ConnectionPool, _Pool((plan_hash, "initial", None, None))),
        plans=_Plans(content),
    )

    with pytest.raises(ServerError) as raised:
        queries.get_plan_preview(
            run_id="run-1",
            version=2,
            after=0,
            limit=50,
        )

    assert raised.value.code is ServerErrorCode.INTERACTION_CONFLICT


class _HistoryCursor:
    def __init__(self, rows: list[tuple[str, bytes]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str, bytes]]:
        return self._rows


class _HistoryConnection:
    def __init__(self, rows: list[tuple[str, bytes]]) -> None:
        self._rows = rows

    def __enter__(self) -> _HistoryConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(
        self,
        query: object,
        parameters: object,
    ) -> _HistoryCursor:
        del query, parameters
        return _HistoryCursor(self._rows)


class _HistoryPool:
    def __init__(self, rows: list[tuple[str, bytes]]) -> None:
        self._rows = rows

    def connection(self) -> _HistoryConnection:
        return _HistoryConnection(self._rows)


class _PreviewConnection(_Connection):
    def execute(
        self,
        query: object,
        parameters: object,
    ) -> _Cursor | _HistoryCursor:
        del parameters
        if "FROM run_events" in str(query):
            return _HistoryCursor([])
        return _Cursor(self._row)


class _PreviewPool(_Pool):
    def connection(self) -> _PreviewConnection:
        return _PreviewConnection(self._row)


def _event(event_type: str, payload: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "event_type": event_type,
            "payload": payload,
            "schema_version": "runtime-event-v1",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _movie_plan_with_unmapped_video() -> MovieRenamePlan:
    snapshot = build_candidate_snapshot(
        (
            ScannedFile(
                PurePosixPath("movie.mkv"),
                CandidateKind.VIDEO,
                10,
                1,
                11,
                12,
                13,
            ),
            ScannedFile(
                PurePosixPath("extra.mkv"),
                CandidateKind.VIDEO,
                10,
                1,
                21,
                22,
                23,
            ),
        )
    )
    mapping = MovieMappingDraft.from_dict(
        {"video_id": "video:1", "subtitle_ids": []},
        candidates=snapshot.candidates,
    )
    draft = compile_movie_plan_draft(
        movie=MovieIdentity("电影", 2024, 99),
        mapping=mapping,
        candidates=snapshot,
        subtitle_variants=(),
    )
    return MovieRenamePlan.create(
        run_id="run-movie",
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
        source_root=RootBinding(PurePosixPath("/watch"), 1, 2),
        output_root=RootBinding(PurePosixPath("/archive"), 1, 3),
        candidate_snapshot=snapshot,
        subtitle_variants=(),
        draft=draft,
        checked_destinations=tuple(
            item.destination for item in draft.moves
        ),
    )


def test_initial_preview_pages_unmapped_items_before_moves() -> None:
    plan = _movie_plan_with_unmapped_video()
    queries = PostgresQueries(
        cast(
            ConnectionPool,
            _PreviewPool((plan.plan_hash, "initial", None, None)),
        ),
        plans=_Plans(plan.canonical_bytes()),
    )

    preview = queries.get_plan_preview(
        run_id=plan.run_id,
        version=1,
        after=0,
        limit=1,
    )

    assert preview is not None
    assert preview["items"][0]["disposition"] == "unmapped"
    assert preview["items"][0]["candidate_id"] == "video:2"
    assert preview["next_after"] == 1


def test_historical_review_reconstructs_verified_inventory_conflict() -> None:
    plan_hash = "sha256:" + "a" * 64
    rows = [
        (
            "archive_directory_listed",
            _event(
                "archive_directory_listed",
                {"occupied": [[0, 1], [0, 2], [0, 3]]},
            ),
        ),
        (
            "mapping_rejected",
            _event(
                "mapping_rejected",
                {
                    "issue": {
                        "code": "inventory_conflict",
                        "context": [
                            {"key": "episode", "value": 3},
                            {"key": "season", "value": 0},
                            {"key": "video_id", "value": "video:13"},
                        ],
                    }
                },
            ),
        ),
        (
            "plan_built",
            _event("plan_built", {"plan_hash": plan_hash}),
        ),
    ]
    queries = PostgresQueries(
        cast(ConnectionPool, _HistoryPool(rows))
    )

    review = queries._historical_plan_review(
        run_id="run-history",
        plan_hash=plan_hash,
        unmapped_ids=frozenset(
            {CandidateId.parse("video:13")}
        ),
    )

    assert review.status is PlanReviewStatus.SYSTEM_ONLY
    assert review.items[0].candidate_id == CandidateId.parse("video:13")
    assert review.items[0].reason is PlanReviewReason.EXISTING_EPISODE
    assert (
        review.items[0].verification
        is PlanReviewVerification.VERIFIED
    )
    assert (review.items[0].season, review.items[0].episode) == (0, 3)


def test_historical_legacy_inventory_observation_replaces_prior_state() -> None:
    plan_hash = "sha256:" + "a" * 64
    rows = [
        (
            "existing_inventory_observed",
            _event(
                "existing_inventory_observed",
                {"occupied": [[0, 3]]},
            ),
        ),
        (
            "existing_inventory_observed",
            _event(
                "existing_inventory_observed",
                {"occupied": [[1, 1]]},
            ),
        ),
        (
            "mapping_rejected",
            _event(
                "mapping_rejected",
                {
                    "issue": {
                        "code": "inventory_conflict",
                        "context": [
                            {"key": "episode", "value": 3},
                            {"key": "season", "value": 0},
                            {"key": "video_id", "value": "video:13"},
                        ],
                    }
                },
            ),
        ),
        (
            "plan_built",
            _event("plan_built", {"plan_hash": plan_hash}),
        ),
    ]
    queries = PostgresQueries(
        cast(ConnectionPool, _HistoryPool(rows))
    )

    review = queries._historical_plan_review(
        run_id="run-history",
        plan_hash=plan_hash,
        unmapped_ids=frozenset(
            {CandidateId.parse("video:13")}
        ),
    )

    assert review.status is PlanReviewStatus.UNAVAILABLE
