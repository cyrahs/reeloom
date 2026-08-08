"""Deterministic projection of durable run facts into the outbox."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Protocol

from reeloom.executor.apply import ApplyResult, ApplyStatus
from reeloom.executor.errors import ExecutorErrorCode
from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.folder_disposition import FolderDispositionAction
from reeloom.kernel.initial_plan import InitialPlan, parse_initial_plan
from reeloom.kernel.movie_plan import MovieRenamePlan
from reeloom.kernel.movie_forward_execution import MovieRenamePlanV2
from reeloom.kernel.rename_plan import RenamePlan
from reeloom.kernel.forward_execution import RenamePlanV2
from reeloom.ports.plans import PlanStore
from reeloom.server.config import ApplyPolicy, ConfigRevision
from reeloom.server.notification_outbox import PostgresNotificationOutbox
from reeloom.server.notifications import (
    ArchiveCompletedNotification,
    AttentionKind,
    AttentionNotification,
    FolderOutcome,
    NotificationSubject,
    NotificationType,
    PlanReadyNotification,
    TmdbPosterRef,
)


class SqlConnection(Protocol):
    def execute(
        self, query: str, params: object = ...
    ) -> object: ...


_ATTENTION_BY_FAILURE = {
    ExecutorErrorCode.DESTINATION_COLLISION.value: AttentionKind.TARGET_EXISTS,
    ExecutorErrorCode.SOURCE_DRIFT.value: AttentionKind.SOURCE_CHANGED,
    ExecutorErrorCode.RECOVERY_REQUIRED.value: (
        AttentionKind.EXECUTION_INTERRUPTED
    ),
}


def _notification_id(dedupe_key: str) -> str:
    digest = hashlib.sha256(dedupe_key.encode("ascii")).hexdigest()
    return f"notification-{digest}"


def _scope(plan: InitialPlan) -> str:
    if isinstance(plan, (MovieRenamePlan, MovieRenamePlanV2)):
        return "电影"
    spans = {
        (move.span.season, move.span.episode_start, move.span.episode_end)
        for move in plan.draft.moves
        if move.source_id.kind is CandidateKind.VIDEO
    }
    seasons = {item[0] for item in spans}
    if len(seasons) == 1 and spans:
        season = next(iter(seasons))
        first = min(item[1] for item in spans)
        last = max(item[2] for item in spans)
        return f"S{season:02d}E{first:02d}–E{last:02d}"
    return f"{len(seasons)} 季 · {len(spans)} 视频"


class PostgresNotificationProjector:
    """Run inside the caller's durable-fact PostgreSQL transaction."""

    def __init__(
        self,
        *,
        plans: PlanStore,
        outbox: PostgresNotificationOutbox,
    ) -> None:
        self._plans = plans
        self._outbox = outbox

    def plan_ready(
        self,
        connection: SqlConnection,
        *,
        run_id: str,
        plan_hash: str,
    ) -> None:
        if not self._enabled(connection, run_id, NotificationType.PLAN_READY):
            return
        plan = self._plan(run_id, plan_hash)
        videos, subtitles = self._counts(plan)
        self._enqueue(
            connection,
            dedupe_key=f"plan_ready:{plan_hash}",
            payload=PlanReadyNotification(
                subject=self._subject(connection, run_id, plan),
                scope_label=_scope(plan),
                video_count=videos,
                subtitle_count=subtitles,
                unmapped_count=len(plan.draft.unmapped_candidate_ids),
                plan_hash=plan_hash,
            ),
        )

    def execution_settled(
        self,
        connection: SqlConnection,
        *,
        run_id: str,
        result: ApplyResult,
    ) -> None:
        if result.status is ApplyStatus.ROLLED_BACK:
            if not self._enabled(
                connection, run_id, NotificationType.ATTENTION_REQUIRED
            ):
                return
            plan = self._plan(run_id, result.plan_hash)
            kind = _ATTENTION_BY_FAILURE.get(
                (
                    None
                    if result.failure_code is None
                    else result.failure_code.value
                ),
                AttentionKind.EXECUTION_ROLLED_BACK,
            )
            self._attention(
                connection,
                run_id=run_id,
                plan=plan,
                kind=kind,
                event_id=result.transaction_id,
            )
            return
        if not self._enabled(
            connection, run_id, NotificationType.ARCHIVE_COMPLETED
        ):
            return
        plan = self._plan(run_id, result.plan_hash)
        folder = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM runs AS r
                JOIN watch_folder_observations AS o
                  ON o.discovery_id = r.discovery_id
                WHERE r.run_id = %s
            )
            """,
            (run_id,),
        ).fetchone()
        if folder is not None and bool(folder[0]):
            return
        self._archive_completed(
            connection,
            run_id=run_id,
            plan=plan,
            transaction_id=result.transaction_id,
            applied_count=result.applied_count,
            folder_outcome=FolderOutcome.NOT_APPLICABLE,
        )

    def folder_settled(
        self,
        connection: SqlConnection,
        *,
        run_id: str,
        approval_id: str,
    ) -> None:
        if not self._enabled(
            connection, run_id, NotificationType.ARCHIVE_COMPLETED
        ):
            return
        row = connection.execute(
            """
            SELECT p.media_plan_hash, p.action,
                   media.transaction_id, media.applied_count
            FROM folder_disposition_approvals AS a
            JOIN folder_disposition_plans AS p
              ON p.run_id = a.run_id AND p.plan_hash = a.plan_hash
            JOIN approvals AS approval
              ON approval.run_id = p.run_id
             AND approval.plan_hash = p.media_plan_hash
            JOIN approval_settlements AS media
              ON media.approval_id = approval.approval_id
             AND media.status = 'completed'
            WHERE a.approval_id = %s AND a.run_id = %s
            """,
            (approval_id, run_id),
        ).fetchone()
        if row is None or row[0] is None:
            return
        action = FolderDispositionAction(str(row[1]))
        self._archive_completed(
            connection,
            run_id=run_id,
            plan=self._plan(run_id, str(row[0])),
            transaction_id=str(row[2]),
            applied_count=int(row[3]),
            folder_outcome=(
                FolderOutcome.REMOVED_EMPTY
                if action is FolderDispositionAction.REMOVE_EMPTY
                else FolderOutcome.ARCHIVED
            ),
        )

    def folder_failed(
        self,
        connection: SqlConnection,
        *,
        run_id: str,
        plan_hash: str,
        event_id: str,
    ) -> None:
        if not self._enabled(
            connection, run_id, NotificationType.ATTENTION_REQUIRED
        ):
            return
        row = connection.execute(
            """
            SELECT media_plan_hash
            FROM folder_disposition_plans
            WHERE run_id = %s AND plan_hash = %s
            """,
            (run_id, plan_hash),
        ).fetchone()
        if row is None or row[0] is None:
            return
        self._attention(
            connection,
            run_id=run_id,
            plan=self._plan(run_id, str(row[0])),
            kind=AttentionKind.FOLDER_DISPOSITION_FAILED,
            event_id=event_id,
        )

    def _archive_completed(
        self,
        connection: SqlConnection,
        *,
        run_id: str,
        plan: InitialPlan,
        transaction_id: str,
        applied_count: int,
        folder_outcome: FolderOutcome,
    ) -> None:
        if not self._enabled(
            connection, run_id, NotificationType.ARCHIVE_COMPLETED
        ):
            return
        self._enqueue(
            connection,
            dedupe_key=f"archive_completed:{transaction_id}",
            payload=ArchiveCompletedNotification(
                subject=self._subject(connection, run_id, plan),
                applied_count=applied_count,
                unmapped_count=len(plan.draft.unmapped_candidate_ids),
                folder_outcome=folder_outcome,
                transaction_id=transaction_id,
            ),
        )

    def _attention(
        self,
        connection: SqlConnection,
        *,
        run_id: str,
        plan: InitialPlan,
        kind: AttentionKind,
        event_id: str,
    ) -> None:
        if not self._enabled(
            connection, run_id, NotificationType.ATTENTION_REQUIRED
        ):
            return
        self._enqueue(
            connection,
            dedupe_key=f"attention_required:{event_id}",
            payload=AttentionNotification(
                subject=self._subject(connection, run_id, plan),
                kind=kind,
                event_id=event_id,
            ),
        )

    def _enabled(
        self,
        connection: SqlConnection,
        run_id: str,
        notification_type: NotificationType,
    ) -> bool:
        row = connection.execute(
            """
            SELECT config.payload
            FROM runs AS run
            JOIN config_revisions AS config
              ON config.revision = run.config_revision
            WHERE run.run_id = %s
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return False
        config = ConfigRevision.from_json(json.dumps(row[0]))
        return (
            config.telegram.enabled
            and notification_type in config.telegram.notification_types
            and not (
                notification_type is NotificationType.PLAN_READY
                and config.apply_policy is ApplyPolicy.AUTOMATIC
            )
        )

    def _plan(self, run_id: str, plan_hash: str) -> InitialPlan:
        plan = parse_initial_plan(
            self._plans.load(plan_hash), plan_hash=plan_hash
        )
        if plan.run_id != run_id:
            raise ValueError("notification plan binding mismatch")
        return plan

    @staticmethod
    def _counts(plan: InitialPlan) -> tuple[int, int]:
        videos = sum(
            source.kind is CandidateKind.VIDEO
            for source in plan.sources
            if source.candidate_id not in plan.draft.unmapped_candidate_ids
        )
        subtitles = len(plan.draft.moves) - videos
        return videos, subtitles

    @staticmethod
    def _subject(
        connection: SqlConnection,
        run_id: str,
        plan: InitialPlan,
    ) -> NotificationSubject:
        row = connection.execute(
            """
            SELECT projection_payload->>'selected_poster_path'
            FROM run_states WHERE run_id = %s
            """,
            (run_id,),
        ).fetchone()
        poster = (
            None
            if row is None or row[0] is None
            else TmdbPosterRef(str(row[0]))
        )
        if isinstance(plan, (RenamePlan, RenamePlanV2)):
            identity = plan.draft.series
            return NotificationSubject(
                title=identity.title_zh_cn,
                year=identity.year,
                work_type=plan.work_type,
                tmdb_id=identity.tmdb_id,
                poster=poster,
            )
        identity = plan.draft.movie
        return NotificationSubject(
            title=identity.title_zh_cn,
            year=identity.release_year,
            work_type=plan.work_type,
            tmdb_id=identity.tmdb_id,
            poster=poster,
        )

    def _enqueue(
        self,
        connection: SqlConnection,
        *,
        dedupe_key: str,
        payload: object,
    ) -> None:
        self._outbox.enqueue_in_transaction(
            connection=connection,
            notification_id=_notification_id(dedupe_key),
            dedupe_key=dedupe_key,
            payload=payload,
            available_at=datetime.now(UTC),
        )
