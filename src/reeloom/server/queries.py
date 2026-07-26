from __future__ import annotations

import json
from typing import Protocol

from psycopg_pool import ConnectionPool

from reeloom.server.config import ConfigRevision
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.executor.manifest import ExecutionManifest
from reeloom.kernel.amendment import verify_amendment_bytes
from reeloom.kernel.rename_plan import verify_plan_bytes


class PlanContentStore(Protocol):
    def load(self, plan_hash: str) -> bytes: ...


def _safe_event(event_type: str, payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    if event_type == "run_started":
        return {"work_type": payload.get("work_type")}
    if event_type == "candidate_snapshot_created":
        return {"candidate_count": payload.get("candidate_count")}
    if event_type == "tmdb_candidates_observed":
        candidates = payload.get("candidates")
        return {
            "candidate_count": (
                len(candidates) if isinstance(candidates, list) else 0
            )
        }
    if event_type == "series_selected":
        series = payload.get("series")
        return {
            "tmdb_id": (
                series.get("tmdb_id")
                if isinstance(series, dict)
                else None
            ),
            "work_type": payload.get("work_type"),
        }
    if event_type == "tmdb_season_catalog_observed":
        return {
            key: payload.get(key)
            for key in ("episode_count", "season_number", "work_type")
        }
    if event_type == "existing_inventory_observed":
        occupied = payload.get("occupied")
        return {
            "occupied_count": (
                len(occupied) if isinstance(occupied, list) else 0
            )
        }
    if event_type == "subtitle_variant_detected":
        return {"variant": payload.get("variant")}
    if event_type == "mapping_rejected":
        issue = payload.get("issue")
        return {
            "code": issue.get("code") if isinstance(issue, dict) else None
        }
    if event_type == "mapping_submitted":
        mapping = payload.get("mapping")
        videos = mapping.get("videos") if isinstance(mapping, dict) else None
        subtitles = (
            mapping.get("subtitles") if isinstance(mapping, dict) else None
        )
        return {
            "video_count": len(videos) if isinstance(videos, list) else 0,
            "subtitle_count": (
                len(subtitles) if isinstance(subtitles, list) else 0
            ),
        }
    allowed = {
        "plan_built": ("plan_hash",),
        "approval_requested": ("plan_hash",),
        "plan_approved": ("approval_id", "plan_hash"),
        "apply_started": ("approval_id", "plan_hash"),
        "apply_failed": ("code",),
        "rollback_completed": ("rolled_back_count", "transaction_id"),
        "run_completed": ("applied_count", "transaction_id"),
        "model_usage_recorded": (
            "input_tokens",
            "output_tokens",
            "total_tokens",
        ),
        "interaction_completed": (
            "interaction_id",
            "kind",
            "model_tokens",
            "model_turns",
            "tool_calls",
            "failures",
            "fresh_mapping_submitted",
            "plan_hash",
        ),
        "execution_settled": (
            "approval_id",
            "transaction_id",
            "status",
            "applied_count",
            "rolled_back_count",
            "failure_code",
            "plan_hash",
        ),
        "tool_requested": ("tool_name",),
        "tool_succeeded": ("tool_name",),
        "tool_rejected": ("tool_name", "code", "retryable"),
        "run_stopped": ("reason",),
        "run_failed": ("code",),
    }
    return {
        key: payload.get(key)
        for key in allowed.get(event_type, ())
    }


class PostgresQueries:
    """Indexed, browser-safe read models; never replay events."""

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        plans: PlanContentStore | None = None,
    ) -> None:
        self._pool = pool
        self._plans = plans

    def get_run(self, run_id: str) -> dict[str, object] | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT r.run_id, r.status, r.work_type,
                           s.phase, s.runtime_status, s.event_sequence,
                           s.model_turns, s.model_tokens, s.tool_calls,
                           s.failures, s.plan_hash,
                           recovery.approval_id,
                           c.payload->>'apply_policy',
                           EXISTS (
                               SELECT 1 FROM run_operations AS operation
                               WHERE operation.run_id = r.run_id
                           ),
                           settlement.approval_id,
                           settlement.plan_hash,
                           settlement.transaction_id,
                           settlement.status,
                           settlement.applied_count,
                           settlement.rolled_back_count,
                           settlement.failure_code,
                           settlement.settled_at
                    FROM runs AS r
                    JOIN config_revisions AS c
                      ON c.revision = r.config_revision
                    LEFT JOIN run_states AS s ON s.run_id = r.run_id
                    LEFT JOIN LATERAL (
                        SELECT c.approval_id
                        FROM approval_claims AS c
                        LEFT JOIN approval_settlements AS settled
                          ON settled.approval_id = c.approval_id
                        WHERE c.run_id = r.run_id
                          AND c.plan_hash = s.plan_hash
                          AND settled.approval_id IS NULL
                        LIMIT 1
                    ) AS recovery ON true
                    LEFT JOIN LATERAL (
                        SELECT a.approval_id, a.plan_hash,
                               settled.transaction_id, settled.status,
                               settled.applied_count,
                               settled.rolled_back_count,
                               settled.failure_code, settled.settled_at
                        FROM approvals AS a
                        JOIN approval_settlements AS settled
                          ON settled.approval_id = a.approval_id
                        WHERE a.run_id = r.run_id
                          AND a.plan_hash = s.plan_hash
                        ORDER BY settled.settled_at DESC
                        LIMIT 1
                    ) AS settlement ON true
                    WHERE r.run_id = %s
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        if row is None:
            return None
        status = str(row[1])
        phase = None if row[3] is None else str(row[3])
        plan_hash = row[10]
        recovery_approval_id = row[11]
        apply_policy = str(row[12])
        busy = bool(row[13])
        actions: list[str] = []
        if not busy and plan_hash is not None:
            if status in {
                "awaiting_approval",
                "completed",
                "rolled_back",
            }:
                actions.append("question")
            if (
                status == "awaiting_approval"
                and phase == "awaiting_approval"
                and recovery_approval_id is None
            ):
                actions.append("revision")
                if apply_policy != "plan_only":
                    actions.append("approve_apply")
            if status == "completed":
                actions.append("reapply")
            if recovery_approval_id is not None:
                actions.append("recover")
        return {
            "run_id": str(row[0]),
            "status": status,
            "work_type": str(row[2]),
            "phase": phase,
            "runtime_status": (
                None if row[4] is None else str(row[4])
            ),
            "event_sequence": 0 if row[5] is None else int(row[5]),
            "model_turns": 0 if row[6] is None else int(row[6]),
            "model_tokens": 0 if row[7] is None else int(row[7]),
            "tool_calls": 0 if row[8] is None else int(row[8]),
            "failures": 0 if row[9] is None else int(row[9]),
            "plan_hash": plan_hash,
            "recovery_approval_id": recovery_approval_id,
            "apply_policy": apply_policy,
            "available_actions": actions,
            "settlement": (
                None
                if row[14] is None
                else {
                    "approval_id": str(row[14]),
                    "plan_hash": str(row[15]),
                    "transaction_id": str(row[16]),
                    "status": str(row[17]),
                    "applied_count": int(row[18]),
                    "rolled_back_count": int(row[19]),
                    "failure_code": (
                        None if row[20] is None else str(row[20])
                    ),
                    "settled_at": row[21].isoformat(),
                }
            ),
        }

    def list_runs(
        self,
        *,
        before: str | None,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    """
                    WITH page_cursor AS (
                        SELECT created_at, run_id
                        FROM runs
                        WHERE run_id = %s
                    )
                    SELECT r.run_id, r.status, r.work_type, r.created_at,
                           s.phase, s.plan_hash
                    FROM runs AS r
                    LEFT JOIN run_states AS s ON s.run_id = r.run_id
                    WHERE (
                        %s::text IS NULL
                        OR (r.created_at, r.run_id) < (
                            SELECT created_at, run_id FROM page_cursor
                        )
                    )
                    ORDER BY r.created_at DESC, r.run_id DESC
                    LIMIT %s
                    """,
                    (before, before, limit),
                ).fetchall()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        return tuple(
            {
                "run_id": str(row[0]),
                "status": str(row[1]),
                "work_type": str(row[2]),
                "created_at": row[3].isoformat(),
                "phase": None if row[4] is None else str(row[4]),
                "plan_hash": row[5],
            }
            for row in rows
        )

    def list_discoveries(
        self,
        *,
        before: str | None,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    """
                    WITH page_cursor AS (
                        SELECT discovered_at, discovery_id
                        FROM discoveries
                        WHERE discovery_id = %s
                    )
                    SELECT d.discovery_id, d.watch_id, d.work_type,
                           d.discovered_at, r.run_id, r.status
                    FROM discoveries AS d
                    LEFT JOIN runs AS r
                      ON r.discovery_id = d.discovery_id
                    WHERE (
                        %s::text IS NULL
                        OR (d.discovered_at, d.discovery_id) < (
                            SELECT discovered_at, discovery_id
                            FROM page_cursor
                        )
                    )
                    ORDER BY d.discovered_at DESC, d.discovery_id DESC
                    LIMIT %s
                    """,
                    (before, before, limit),
                ).fetchall()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        return tuple(
            {
                "discovery_id": str(row[0]),
                "watch_id": str(row[1]),
                "work_type": str(row[2]),
                "discovered_at": row[3].isoformat(),
                "run_id": None if row[4] is None else str(row[4]),
                "run_status": None if row[5] is None else str(row[5]),
            }
            for row in rows
        )

    def list_events(
        self,
        *,
        run_id: str,
        after_event_id: int,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT event_id, event_type, payload
                    FROM run_events
                    WHERE run_id = %s AND event_id > %s
                    ORDER BY event_id
                    LIMIT %s
                    """,
                    (run_id, after_event_id, limit),
                ).fetchall()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        result = []
        for row in rows:
            envelope = json.loads(bytes(row[2]))
            result.append(
                {
                    "event_id": int(row[0]),
                    "event_type": str(row[1]),
                    "data": _safe_event(
                        str(row[1]),
                        envelope.get("payload"),
                    ),
                }
            )
        return tuple(result)

    def latest_event_id(self, run_id: str) -> int:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT COALESCE(max(event_id), 0)
                    FROM run_events
                    WHERE run_id = %s
                    """,
                    (run_id,),
                ).fetchone()
                return int(row[0])
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def get_config(self) -> dict[str, object] | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT r.payload
                    FROM config_heads AS h
                    JOIN config_revisions AS r USING (revision)
                    WHERE h.singleton = true
                    """
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        if row is None:
            return None
        value = row[0] if isinstance(row[0], dict) else json.loads(str(row[0]))
        return ConfigRevision.from_json(
            json.dumps(value, ensure_ascii=False)
        ).public_payload()

    def get_plan(
        self,
        *,
        run_id: str,
        version: int | None,
    ) -> dict[str, object] | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT l.version, l.plan_hash, l.parent_plan_hash,
                           l.plan_kind, l.created_at
                    FROM plan_lineage AS l
                    LEFT JOIN plan_heads AS h
                      ON h.run_id = l.run_id AND h.version = l.version
                    WHERE l.run_id = %s
                      AND (
                        (%s::integer IS NULL AND h.run_id IS NOT NULL)
                        OR l.version = %s
                      )
                    """,
                    (run_id, version, version),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        if row is None:
            return None
        return {
            "run_id": run_id,
            "version": int(row[0]),
            "plan_hash": str(row[1]),
            "parent_plan_hash": row[2],
            "plan_kind": str(row[3]),
            "created_at": row[4].isoformat(),
        }

    def list_plans(
        self,
        *,
        run_id: str,
        before_version: int | None,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT version, plan_hash, parent_plan_hash,
                           plan_kind, created_at
                    FROM plan_lineage
                    WHERE run_id = %s
                      AND (%s::integer IS NULL OR version < %s)
                    ORDER BY version DESC
                    LIMIT %s
                    """,
                    (run_id, before_version, before_version, limit),
                ).fetchall()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        return tuple(
            {
                "run_id": run_id,
                "version": int(row[0]),
                "plan_hash": str(row[1]),
                "parent_plan_hash": row[2],
                "plan_kind": str(row[3]),
                "created_at": row[4].isoformat(),
            }
            for row in rows
        )

    def get_plan_preview(
        self,
        *,
        run_id: str,
        version: int,
        after: int,
        limit: int,
    ) -> dict[str, object] | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT plan_hash, plan_kind
                    FROM plan_lineage
                    WHERE run_id = %s AND version = %s
                    """,
                    (run_id, version),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        if row is None:
            return None
        if self._plans is None:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        plan_hash = str(row[0])
        plan_kind = str(row[1])
        if plan_kind not in {"initial", "amendment"}:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            canonical_bytes = self._plans.load(plan_hash)
            is_amendment = verify_amendment_bytes(
                canonical_bytes,
                plan_hash,
            )
            if plan_kind == "initial":
                valid_kind = (
                    not is_amendment
                    and verify_plan_bytes(canonical_bytes, plan_hash)
                )
            else:
                valid_kind = is_amendment
            if not valid_kind:
                raise ServerError(
                    ServerErrorCode.INTERACTION_CONFLICT
                )
            manifest = ExecutionManifest.from_canonical_bytes(
                canonical_bytes,
                plan_hash=plan_hash,
            )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.INTERACTION_CONFLICT
            ) from None
        if manifest.run_id != run_id:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        sources = {
            source.candidate_id: source for source in manifest.sources
        }
        moved = {move.source_id for move in manifest.moves}
        items: list[dict[str, object]] = []
        for move in manifest.moves:
            source = sources[move.source_id]
            items.append(
                {
                    "disposition": "move",
                    "candidate_id": str(source.candidate_id),
                    "kind": source.kind.value,
                    "source": source.relative_path.as_posix(),
                    "destination": move.destination.as_posix(),
                }
            )
        remaining_disposition = (
            "unmapped" if plan_kind == "initial" else "unchanged"
        )
        for source in manifest.sources:
            if source.candidate_id in moved:
                continue
            items.append(
                {
                    "disposition": remaining_disposition,
                    "candidate_id": str(source.candidate_id),
                    "kind": source.kind.value,
                    "source": source.relative_path.as_posix(),
                    "destination": None,
                }
            )
        indexed = [
            {"index": index, **item}
            for index, item in enumerate(items)
        ]
        page = indexed[after : after + limit]
        next_after = after + len(page)
        counts = {
            "move": len(manifest.moves),
            "unmapped": (
                len(manifest.sources) - len(manifest.moves)
                if plan_kind == "initial"
                else 0
            ),
            "unchanged": (
                len(manifest.sources) - len(manifest.moves)
                if plan_kind == "amendment"
                else 0
            ),
        }
        return {
            "run_id": run_id,
            "version": version,
            "plan_hash": plan_hash,
            "plan_kind": plan_kind,
            "counts": counts,
            "items": page,
            "next_after": next_after if next_after < len(indexed) else None,
        }

    def list_interactions(
        self,
        *,
        run_id: str,
        before: str | None,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        try:
            with self._pool.connection() as connection:
                cursor = None
                if before is not None:
                    cursor = connection.execute(
                        """
                        SELECT created_at, interaction_id
                        FROM interactions
                        WHERE run_id = %s AND interaction_id = %s
                        """,
                        (run_id, before),
                    ).fetchone()
                    if cursor is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_NOT_FOUND
                        )
                rows = connection.execute(
                    """
                    SELECT interaction_id, kind, status, request_message,
                           result, created_at, finished_at
                    FROM interactions
                    WHERE run_id = %s
                      AND (
                        %s::timestamptz IS NULL
                        OR (created_at, interaction_id) < (%s, %s)
                      )
                    ORDER BY created_at DESC, interaction_id DESC
                    LIMIT %s
                    """,
                    (
                        run_id,
                        None if cursor is None else cursor[0],
                        None if cursor is None else cursor[0],
                        None if cursor is None else cursor[1],
                        limit,
                    ),
                ).fetchall()
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        result: list[dict[str, object]] = []
        for row in rows:
            raw_result = (
                None
                if row[4] is None
                else row[4]
                if isinstance(row[4], dict)
                else json.loads(str(row[4]))
            )
            message = row[3]
            result.append(
                {
                    "interaction_id": str(row[0]),
                    "kind": str(row[1]),
                    "status": str(row[2]),
                    "request_message": message,
                    "assistant_reply": (
                        raw_result.get("assistant_reply")
                        if isinstance(raw_result, dict)
                        else None
                    ),
                    "content_available": message is not None,
                    "plan_hash": (
                        raw_result.get("plan_hash")
                        if isinstance(raw_result, dict)
                        else None
                    ),
                    "created_at": row[5].isoformat(),
                    "finished_at": (
                        None if row[6] is None else row[6].isoformat()
                    ),
                }
            )
        return tuple(result)
