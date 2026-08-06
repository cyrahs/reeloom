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
from reeloom.server.queries import PostgresQueries, _safe_event


class _Cursor:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...]:
        return self._row


class _Connection:
    def __init__(
        self,
        row: tuple[object, ...],
        required_query_fragment: str | None = None,
    ) -> None:
        self._row = row
        self._required_query_fragment = required_query_fragment

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(
        self,
        query: object,
        parameters: object,
    ) -> _Cursor:
        if self._required_query_fragment is not None:
            assert self._required_query_fragment in str(query)
        del query, parameters
        return _Cursor(self._row)


class _Pool:
    def __init__(
        self,
        row: tuple[object, ...],
        required_query_fragment: str | None = None,
    ) -> None:
        self._row = row
        self._required_query_fragment = required_query_fragment

    def connection(self) -> _Connection:
        return _Connection(self._row, self._required_query_fragment)


class _Plans:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def load(self, plan_hash: str) -> bytes:
        del plan_hash
        return self._content


def _completed_run_row(
    *,
    layout_matches_current_plan: bool,
    interaction_budget_available: bool = True,
) -> tuple[object, ...]:
    row: list[object] = [None] * 50
    row[:14] = (
        "run-1",
        "completed",
        "anime",
        "completed",
        "completed",
        1,
        0,
        0,
        0,
        0,
        "sha256:" + "a" * 64,
        None,
        "manual",
        False,
    )
    row[32] = False
    row[35] = layout_matches_current_plan
    row[36] = interaction_budget_available
    row[46] = 3
    row[47] = False
    row[48] = "completed"
    return tuple(row)


def test_needs_attention_exposes_bounded_preplan_controls() -> None:
    row = list(
        _completed_run_row(
            layout_matches_current_plan=False,
            interaction_budget_available=True,
        )
    )
    row[1] = "running"
    row[3] = "map_episodes"
    row[4] = "stopped"
    row[10] = None
    row[44] = "needs_attention"
    row[45] = True
    row[46] = 1
    row[47] = True
    queries = PostgresQueries(cast(ConnectionPool, _Pool(tuple(row))))

    run = queries.get_run("run-1")

    assert run is not None
    assert run["status"] == "needs_attention"
    assert run["available_actions"] == [
        "question",
        "retry_run",
        "fail_run",
    ]


def test_needs_attention_retry_is_hidden_after_budget_is_exhausted() -> None:
    row = list(
        _completed_run_row(
            layout_matches_current_plan=False,
            interaction_budget_available=False,
        )
    )
    row[1] = "running"
    row[3] = "map_episodes"
    row[4] = "stopped"
    row[10] = None
    row[44] = "needs_attention"
    row[45] = True
    row[46] = 3
    row[47] = True
    queries = PostgresQueries(cast(ConnectionPool, _Pool(tuple(row))))

    run = queries.get_run("run-1")

    assert run is not None
    assert run["available_actions"] == ["fail_run"]


def test_manual_subtitle_acquisition_exposes_only_independent_action() -> None:
    row = list(
        _completed_run_row(
            layout_matches_current_plan=False,
            interaction_budget_available=False,
        )
    )
    row[1] = "running"
    row[3] = "build_subtitle_acquisition_plan"
    row[10] = None
    row[37:43] = (
        "sha256:" + "b" * 64,
        "manual",
        "planned",
        None,
        None,
        None,
    )
    queries = PostgresQueries(cast(ConnectionPool, _Pool(tuple(row))))

    run = queries.get_run("run-1")

    assert run is not None
    assert run["available_actions"] == ["approve_subtitle_acquisition"]
    assert run["subtitle_acquisition"] == {
        "plan_hash": "sha256:" + "b" * 64,
        "policy": "manual",
        "status": "planned",
        "approval_id": None,
        "transaction_id": None,
        "failure_code": None,
        "failure_diagnostic": None,
        "successor_status": None,
    }


@pytest.mark.parametrize("policy", ("plan_only", "automatic"))
def test_nonmanual_subtitle_acquisition_has_no_browser_action(
    policy: str,
) -> None:
    row = list(_completed_run_row(layout_matches_current_plan=False))
    row[1] = "running"
    row[3] = "build_subtitle_acquisition_plan"
    row[10] = None
    row[37:43] = (
        "sha256:" + "b" * 64,
        policy,
        "planned",
        None,
        None,
        None,
    )
    queries = PostgresQueries(cast(ConnectionPool, _Pool(tuple(row))))

    run = queries.get_run("run-1")

    assert run is not None
    assert "approve_subtitle_acquisition" not in run["available_actions"]


def test_blocked_subtitle_acquisition_has_terminal_attention_action() -> None:
    row = list(_completed_run_row(layout_matches_current_plan=False))
    row[1] = "running"
    row[3] = "build_subtitle_acquisition_plan"
    row[10] = None
    row[37:43] = (
        "sha256:" + "b" * 64,
        "automatic",
        "blocked",
        "approval-subtitle-1",
        None,
        "destination_collision",
    )
    row[45] = True
    row[47] = True
    row[49] = {
        "schema_version": 1,
        "stage": "staging_validate",
        "reason": "unsafe_permissions",
        "actual_mode": 0o775,
        "expected_policy": "owner_rwx_no_group_or_other_write",
    }
    queries = PostgresQueries(cast(ConnectionPool, _Pool(tuple(row))))

    run = queries.get_run("run-1")

    assert run is not None
    assert run["status"] == "needs_attention"
    assert run["available_actions"] == [
        "retry_subtitle_acquisition",
        "fail_run",
    ]
    assert run["subtitle_acquisition"]["failure_diagnostic"] == row[49]


def test_noncollision_subtitle_failure_is_not_retryable() -> None:
    row = list(_completed_run_row(layout_matches_current_plan=False))
    row[1] = "running"
    row[3] = "build_subtitle_acquisition_plan"
    row[10] = None
    row[37:43] = (
        "sha256:" + "b" * 64,
        "automatic",
        "blocked",
        "approval-subtitle-1",
        None,
        "source_drift",
    )
    row[45] = True
    row[47] = True
    queries = PostgresQueries(cast(ConnectionPool, _Pool(tuple(row))))

    run = queries.get_run("run-1")

    assert run is not None
    assert run["available_actions"] == ["fail_run"]


def test_pending_job_hides_manual_subtitle_action() -> None:
    row = list(_completed_run_row(layout_matches_current_plan=False))
    row[1] = "running"
    row[3] = "build_subtitle_acquisition_plan"
    row[10] = None
    row[37:43] = (
        "sha256:" + "b" * 64,
        "manual",
        "planned",
        None,
        None,
        None,
    )
    row[48] = "pending"
    queries = PostgresQueries(cast(ConnectionPool, _Pool(tuple(row))))

    run = queries.get_run("run-1")

    assert run is not None
    assert run["available_actions"] == []


def test_manual_approved_subtitle_request_exposes_recovery_action() -> None:
    row = list(_completed_run_row(layout_matches_current_plan=False))
    row[1] = "running"
    row[3] = "build_subtitle_acquisition_plan"
    row[10] = None
    row[37:43] = (
        "sha256:" + "b" * 64,
        "manual",
        "approved",
        "approval-subtitle-1",
        None,
        None,
    )
    queries = PostgresQueries(cast(ConnectionPool, _Pool(tuple(row))))

    run = queries.get_run("run-1")

    assert run is not None
    assert "approve_subtitle_acquisition" in run["available_actions"]


def test_blocked_successor_is_exposed_as_bounded_attention_state() -> None:
    row = list(_completed_run_row(layout_matches_current_plan=False))
    row[1] = "superseded"
    row[37:44] = (
        "sha256:" + "b" * 64,
        "automatic",
        "published",
        "approval-subtitle-1",
        "subtitle-txn-v1-" + "c" * 64,
        None,
        "blocked",
    )
    queries = PostgresQueries(cast(ConnectionPool, _Pool(tuple(row))))

    run = queries.get_run("run-1")

    assert run is not None
    assert run["subtitle_acquisition"]["successor_status"] == "blocked"
    assert "approve_subtitle_acquisition" not in run["available_actions"]


@pytest.mark.parametrize(
    "layout_matches_current_plan",
    (False, True),
    ids=("missing-or-mismatched", "matching"),
)
def test_completed_run_exposes_reapply_only_for_current_layout(
    layout_matches_current_plan: bool,
) -> None:
    queries = PostgresQueries(
        cast(
            ConnectionPool,
            _Pool(
                _completed_run_row(
                    layout_matches_current_plan=(
                        layout_matches_current_plan
                    )
                ),
                "layout.plan_hash = s.plan_hash",
            ),
        )
    )

    run = queries.get_run("run-1")

    assert run is not None
    assert (
        "reapply" in run["available_actions"]
    ) is layout_matches_current_plan


def test_completed_run_hides_interactions_when_budget_is_exhausted() -> None:
    queries = PostgresQueries(
        cast(
            ConnectionPool,
            _Pool(
                _completed_run_row(
                    layout_matches_current_plan=True,
                    interaction_budget_available=False,
                )
            ),
        )
    )

    run = queries.get_run("run-1")

    assert run is not None
    assert "question" not in run["available_actions"]
    assert "reapply" not in run["available_actions"]


def test_exhausted_interaction_budget_does_not_hide_apply() -> None:
    row = list(
        _completed_run_row(
            layout_matches_current_plan=False,
            interaction_budget_available=False,
        )
    )
    row[1] = "awaiting_approval"
    row[3] = "awaiting_approval"
    queries = PostgresQueries(
        cast(ConnectionPool, _Pool(tuple(row)))
    )

    run = queries.get_run("run-1")

    assert run is not None
    assert run["available_actions"] == ["approve_apply"]


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


def test_subtitle_search_event_exposes_only_bounded_diagnostics() -> None:
    value = _safe_event(
        "subtitle_search_observed",
        {
            "record": {
                "season_number": 1,
                "page": {"items": []},
            },
            "capabilities": [],
            "diagnostics": {
                "query_aliases": ["我的百合乃工作是也！"],
                "alias_thread_counts": [0],
                "discovered_thread_count": 0,
                "fetched_thread_count": 0,
                "fetched_thread_page_count": 0,
                "parsed_post_count": 0,
                "native_attachment_count": 0,
                "selectable_archive_set_count": 0,
                "empty_stage": "forum_search",
            },
        },
    )

    assert value == {
        "season_number": 1,
        "query_aliases": "我的百合乃工作是也！",
        "alias_thread_counts": "我的百合乃工作是也！=0",
        "discovered_thread_count": 0,
        "fetched_thread_count": 0,
        "fetched_thread_page_count": 0,
        "parsed_post_count": 0,
        "native_attachment_count": 0,
        "selectable_archive_set_count": 0,
        "release_count": 0,
        "archive_set_count": 0,
        "empty_stage": "forum_search",
    }


def test_subtitle_search_failure_exposes_bounded_diagnostics() -> None:
    value = _safe_event(
        "subtitle_search_failed",
        {
            "call_id": "model-controlled-call-id",
            "reason_code": "subtitle_search_unavailable",
            "season_number": 1,
            "diagnostics": {
                "error_code": "challenge_or_login",
                "stage": "forum_search",
                "retryable": False,
                "query_aliases": [
                    "我的百合乃工作是也!",
                    "我的百合乃工作是也",
                ],
                "query_alias_index": 1,
                "http_response_count": 4,
                "received_html_bytes": 32_768,
                "http_status": 200,
            },
        },
    )

    assert value == {
        "reason_code": "subtitle_search_unavailable",
        "season_number": 1,
        "error_code": "challenge_or_login",
        "failure_stage": "forum_search",
        "retryable": False,
        "query_aliases": "我的百合乃工作是也! | 我的百合乃工作是也",
        "failed_query_alias": "我的百合乃工作是也",
        "http_response_count": 4,
        "received_html_bytes": 32_768,
        "http_status": 200,
    }
    assert "call_id" not in value


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
