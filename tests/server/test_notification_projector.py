from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import PurePosixPath

from reeloom.executor.apply import ApplyResult, ApplyStatus
from reeloom.executor.errors import ExecutorErrorCode
from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.movie import (
    MovieMappingDraft,
    compile_movie_plan_draft,
)
from reeloom.kernel.movie_plan import MovieRenamePlan
from reeloom.kernel.naming import MovieIdentity
from reeloom.kernel.rename_plan import RootBinding
from reeloom.kernel.scanner import ScannedFile, build_candidate_snapshot
from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
    TelegramConfig,
)
from reeloom.server.notification_projector import (
    PostgresNotificationProjector,
)
from reeloom.server.notifications import (
    ArchiveCompletedNotification,
    AttentionKind,
    AttentionNotification,
    FolderOutcome,
    PlanReadyNotification,
)


class _Cursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _Connection:
    def __init__(
        self,
        config: ConfigRevision,
        *,
        poster_path: str | None = "/poster.jpg",
        folder: bool = False,
    ) -> None:
        self._config = json.loads(config.to_json())
        self._poster_path = poster_path
        self._folder = folder

    def execute(
        self, query: str, params: object = None
    ) -> _Cursor:
        del params
        if "SELECT config.payload" in query:
            return _Cursor((self._config,))
        if "selected_poster_path" in query:
            return _Cursor((self._poster_path,))
        if "SELECT EXISTS" in query:
            return _Cursor((self._folder,))
        raise AssertionError(query)


class _Plans:
    def __init__(self, plan: MovieRenamePlan) -> None:
        self._plan = plan

    def load(self, plan_hash: str) -> bytes:
        assert plan_hash == self._plan.plan_hash
        return self._plan.canonical_bytes()


class _Outbox:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def enqueue_in_transaction(self, **kwargs: object) -> None:
        self.items.append(dict(kwargs))


def _plan() -> MovieRenamePlan:
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
        movie=MovieIdentity("测试电影", 2024, 99),
        mapping=mapping,
        candidates=snapshot,
        subtitle_variants=(),
    )
    return MovieRenamePlan.create(
        run_id="run-movie",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        source_root=RootBinding(PurePosixPath("/watch"), 1, 2),
        output_root=RootBinding(PurePosixPath("/archive"), 1, 3),
        candidate_snapshot=snapshot,
        subtitle_variants=(),
        draft=draft,
        checked_destinations=tuple(
            item.destination for item in draft.moves
        ),
    )


def _config(
    *,
    enabled: bool = True,
    apply_policy: ApplyPolicy = ApplyPolicy.MANUAL,
) -> ConfigRevision:
    return ConfigRevision.create(
        revision_id="cfg-1",
        revision=1,
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        draft=ConfigDraft(
            watches=(),
            provider=ProviderConfig(
                base_url="https://api.openai.com/v1",
                model="test",
                secret_ref="provider-secret",
            ),
            apply_policy=apply_policy,
            telegram=TelegramConfig(
                enabled=enabled,
                chat_id="123",
                secret_ref="telegram-secret",
            ),
        ),
    )


def _projector(plan: MovieRenamePlan) -> tuple[PostgresNotificationProjector, _Outbox]:
    outbox = _Outbox()
    return (
        PostgresNotificationProjector(
            plans=_Plans(plan),  # type: ignore[arg-type]
            outbox=outbox,  # type: ignore[arg-type]
        ),
        outbox,
    )


def test_plan_ready_projects_counts_and_tmdb_poster() -> None:
    plan = _plan()
    projector, outbox = _projector(plan)

    projector.plan_ready(
        _Connection(_config()),
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
    )

    assert len(outbox.items) == 1
    item = outbox.items[0]
    payload = item["payload"]
    assert isinstance(payload, PlanReadyNotification)
    assert payload.video_count == 1
    assert payload.subtitle_count == 0
    assert payload.unmapped_count == 1
    assert payload.scope_label == "电影"
    assert payload.subject.poster is not None
    assert payload.subject.poster.url.endswith("/poster.jpg")
    assert item["dedupe_key"] == f"plan_ready:{plan.plan_hash}"


def test_disabled_config_produces_no_outbox_row() -> None:
    plan = _plan()
    projector, outbox = _projector(plan)

    projector.plan_ready(
        _Connection(_config(enabled=False)),
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
    )

    assert outbox.items == []


def test_automatic_policy_suppresses_only_plan_ready_notification() -> None:
    plan = _plan()
    projector, outbox = _projector(plan)
    connection = _Connection(
        _config(apply_policy=ApplyPolicy.AUTOMATIC),
        folder=False,
    )

    projector.plan_ready(
        connection,
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
    )

    assert outbox.items == []

    projector.execution_settled(
        connection,
        run_id=plan.run_id,
        result=ApplyResult(
            transaction_id="txn-automatic-completed",
            plan_hash=plan.plan_hash,
            approval_id="approval-automatic",
            status=ApplyStatus.COMPLETED,
            applied_count=1,
            rolled_back_count=0,
            failure_code=None,
        ),
    )

    assert len(outbox.items) == 1
    assert isinstance(
        outbox.items[0]["payload"], ArchiveCompletedNotification
    )


def test_completion_waits_for_folder_and_rollback_requests_attention() -> None:
    plan = _plan()
    projector, outbox = _projector(plan)
    completed = ApplyResult(
        transaction_id="txn-completed",
        plan_hash=plan.plan_hash,
        approval_id="approval-1",
        status=ApplyStatus.COMPLETED,
        applied_count=1,
        rolled_back_count=0,
        failure_code=None,
    )

    projector.execution_settled(
        _Connection(_config(), folder=True),
        run_id=plan.run_id,
        result=completed,
    )
    assert outbox.items == []

    projector.execution_settled(
        _Connection(_config(), folder=False),
        run_id=plan.run_id,
        result=completed,
    )
    archived = outbox.items[-1]["payload"]
    assert isinstance(archived, ArchiveCompletedNotification)
    assert archived.folder_outcome is FolderOutcome.NOT_APPLICABLE

    projector.execution_settled(
        _Connection(_config()),
        run_id=plan.run_id,
        result=ApplyResult(
            transaction_id="txn-rollback",
            plan_hash=plan.plan_hash,
            approval_id="approval-2",
            status=ApplyStatus.ROLLED_BACK,
            applied_count=0,
            rolled_back_count=1,
            failure_code=ExecutorErrorCode.SOURCE_DRIFT,
        ),
    )
    attention = outbox.items[-1]["payload"]
    assert isinstance(attention, AttentionNotification)
    assert attention.kind is AttentionKind.SOURCE_CHANGED
    assert outbox.items[-1]["dedupe_key"] == (
        "attention_required:txn-rollback"
    )
