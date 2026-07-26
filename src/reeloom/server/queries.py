from __future__ import annotations

import json

from psycopg_pool import ConnectionPool

from reeloom.server.config import ConfigRevision
from reeloom.server.errors import ServerError, ServerErrorCode


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

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def get_run(self, run_id: str) -> dict[str, object] | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT r.run_id, r.status, r.work_type,
                           s.phase, s.runtime_status, s.event_sequence,
                           s.model_turns, s.model_tokens, s.tool_calls,
                           s.failures, s.plan_hash,
                           recovery.approval_id
                    FROM runs AS r
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
        return {
            "run_id": str(row[0]),
            "status": str(row[1]),
            "work_type": str(row[2]),
            "phase": None if row[3] is None else str(row[3]),
            "runtime_status": (
                None if row[4] is None else str(row[4])
            ),
            "event_sequence": 0 if row[5] is None else int(row[5]),
            "model_turns": 0 if row[6] is None else int(row[6]),
            "model_tokens": 0 if row[7] is None else int(row[7]),
            "tool_calls": 0 if row[8] is None else int(row[8]),
            "failures": 0 if row[9] is None else int(row[9]),
            "plan_hash": row[10],
            "recovery_approval_id": row[11],
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
                    SELECT run_id, status, work_type, created_at
                    FROM runs
                    WHERE (%s::text IS NULL OR run_id < %s)
                    ORDER BY run_id DESC
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
                    SELECT discovery_id, watch_id, work_type, discovered_at
                    FROM discoveries
                    WHERE (%s::text IS NULL OR discovery_id < %s)
                    ORDER BY discovery_id DESC
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
