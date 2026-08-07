from __future__ import annotations

import json
from typing import Protocol

from psycopg_pool import ConnectionPool

from reeloom.executor.manifest import ExecutionManifest
from reeloom.kernel.amendment import verify_amendment_bytes
from reeloom.kernel.initial_plan import verify_initial_plan_bytes
from reeloom.kernel.movie_amendment import (
    verify_movie_amendment_bytes,
)
from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.forward_execution import ExecutionOperationStatus
from reeloom.kernel.errors import DomainError
from reeloom.kernel.plan_review import (
    PLAN_REVIEW_SCHEMA,
    PlanReview,
    PlanReviewItem,
    PlanReviewReason,
    PlanReviewVerification,
    merge_plan_reviews,
)
from reeloom.server.archive_report import archive_report_from_projection
from reeloom.server.config import ApplyPolicy, ConfigRevision
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.forward_actions import forward_available_actions
from reeloom.server.run_deletion_policy import RUN_DELETION_READY_SQL


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
    if event_type == "movie_selected":
        movie = payload.get("movie")
        return {
            "tmdb_id": (
                movie.get("tmdb_id")
                if isinstance(movie, dict)
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
    if event_type == "archive_search_observed":
        directories = payload.get("directory_ids")
        return {
            "match_count": (
                len(directories) if isinstance(directories, list) else 0
            ),
            "complete": payload.get("complete"),
            "work_type": payload.get("work_type"),
        }
    if event_type == "archive_directory_listed":
        children = payload.get("child_ids")
        videos = payload.get("videos")
        return {
            "directory_count": (
                len(children) if isinstance(children, list) else 0
            ),
            "video_count": (
                len(videos) if isinstance(videos, list) else 0
            ),
            "complete": payload.get("complete"),
        }
    if event_type == "subtitle_variant_detected":
        return {"variant": payload.get("variant")}
    if event_type == "embedded_subtitles_inspected":
        inspection = payload.get("inspection")
        tracks = (
            inspection.get("tracks")
            if isinstance(inspection, dict)
            else None
        )
        return {
            "chinese_status": (
                inspection.get("chinese_status")
                if isinstance(inspection, dict)
                else None
            ),
            "probe_status": (
                inspection.get("probe_status")
                if isinstance(inspection, dict)
                else None
            ),
            "season_number": (
                inspection.get("season_number")
                if isinstance(inspection, dict)
                else None
            ),
            "track_count": len(tracks) if isinstance(tracks, list) else 0,
        }
    if event_type == "subtitle_search_observed":
        record = payload.get("record")
        page = record.get("page") if isinstance(record, dict) else None
        releases = page.get("items") if isinstance(page, dict) else None
        capabilities = payload.get("capabilities")
        diagnostics = payload.get("diagnostics")
        aliases = (
            diagnostics.get("query_aliases")
            if isinstance(diagnostics, dict)
            else None
        )
        alias_counts = (
            diagnostics.get("alias_thread_counts")
            if isinstance(diagnostics, dict)
            else None
        )
        alias_pairs = (
            [
                f"{alias}={count}"
                for alias, count in zip(aliases, alias_counts, strict=True)
                if isinstance(alias, str) and type(count) is int
            ]
            if isinstance(aliases, list)
            and isinstance(alias_counts, list)
            and len(aliases) == len(alias_counts)
            else []
        )
        return {
            "season_number": (
                record.get("season_number")
                if isinstance(record, dict)
                else None
            ),
            "query_aliases": (
                " | ".join(
                    alias for alias in aliases if isinstance(alias, str)
                )
                if isinstance(aliases, list)
                else None
            ),
            "alias_thread_counts": " | ".join(alias_pairs),
            "discovered_thread_count": (
                diagnostics.get("discovered_thread_count")
                if isinstance(diagnostics, dict)
                else None
            ),
            "fetched_thread_count": (
                diagnostics.get("fetched_thread_count")
                if isinstance(diagnostics, dict)
                else None
            ),
            "fetched_thread_page_count": (
                diagnostics.get("fetched_thread_page_count")
                if isinstance(diagnostics, dict)
                else None
            ),
            "parsed_post_count": (
                diagnostics.get("parsed_post_count")
                if isinstance(diagnostics, dict)
                else None
            ),
            "native_attachment_count": (
                diagnostics.get("native_attachment_count")
                if isinstance(diagnostics, dict)
                else None
            ),
            "selectable_archive_set_count": (
                diagnostics.get("selectable_archive_set_count")
                if isinstance(diagnostics, dict)
                else None
            ),
            "release_count": (
                len(releases) if isinstance(releases, list) else 0
            ),
            "archive_set_count": (
                len(capabilities) if isinstance(capabilities, list) else 0
            ),
            "empty_stage": (
                diagnostics.get("empty_stage")
                if isinstance(diagnostics, dict)
                else None
            ),
        }
    if event_type == "subtitle_search_failed":
        diagnostics = payload.get("diagnostics")
        aliases = (
            diagnostics.get("query_aliases")
            if isinstance(diagnostics, dict)
            else None
        )
        alias_index = (
            diagnostics.get("query_alias_index")
            if isinstance(diagnostics, dict)
            else None
        )
        failed_alias = (
            aliases[alias_index]
            if isinstance(aliases, list)
            and type(alias_index) is int
            and 0 <= alias_index < len(aliases)
            and isinstance(aliases[alias_index], str)
            else None
        )
        return {
            "reason_code": payload.get("reason_code"),
            "season_number": payload.get("season_number"),
            "error_code": (
                diagnostics.get("error_code")
                if isinstance(diagnostics, dict)
                else None
            ),
            "failure_stage": (
                diagnostics.get("stage")
                if isinstance(diagnostics, dict)
                else None
            ),
            "retryable": (
                diagnostics.get("retryable")
                if isinstance(diagnostics, dict)
                else None
            ),
            "query_aliases": (
                " | ".join(
                    alias for alias in aliases if isinstance(alias, str)
                )
                if isinstance(aliases, list)
                else None
            ),
            "failed_query_alias": failed_alias,
            "http_response_count": (
                diagnostics.get("http_response_count")
                if isinstance(diagnostics, dict)
                else None
            ),
            "received_html_bytes": (
                diagnostics.get("received_html_bytes")
                if isinstance(diagnostics, dict)
                else None
            ),
            "http_status": (
                diagnostics.get("http_status")
                if isinstance(diagnostics, dict)
                else None
            ),
        }
    if event_type == "subtitle_selection_submitted":
        decision = payload.get("decision")
        selections = (
            decision.get("selections")
            if isinstance(decision, dict)
            else None
        )
        return {
            "reason_code": (
                decision.get("reason_code")
                if isinstance(decision, dict)
                else None
            ),
            "selection_count": (
                len(selections) if isinstance(selections, list) else 0
            ),
            "status": (
                decision.get("status")
                if isinstance(decision, dict)
                else None
            ),
        }
    if event_type == "mapping_rejected":
        issue = payload.get("issue")
        return {
            "code": issue.get("code") if isinstance(issue, dict) else None
        }
    if event_type == "mapping_review_captured":
        review = payload.get("review")
        items = review.get("items") if isinstance(review, dict) else None
        return {
            "status": (
                review.get("status")
                if isinstance(review, dict)
                else None
            ),
            "item_count": len(items) if isinstance(items, list) else 0,
            "verified_count": (
                sum(
                    item.get("verification") == "verified"
                    for item in items
                    if isinstance(item, dict)
                )
                if isinstance(items, list)
                else 0
            ),
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
    if event_type == "movie_mapping_submitted":
        mapping = payload.get("mapping")
        subtitles = (
            mapping.get("subtitle_ids")
            if isinstance(mapping, dict)
            else None
        )
        return {
            "video_count": (
                1
                if isinstance(mapping, dict)
                and isinstance(mapping.get("video_id"), str)
                else 0
            ),
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
                    f"""
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
                           settlement.settled_at,
                           d.source_folder,
                           folder.plan_hash,
                           folder.action,
                           folder.target_relative,
                           folder.file_count,
                           folder.reason_code,
                           folder.status,
                           folder.approval_id,
                           folder.failure_code,
                           folder.move_backend,
                           ({RUN_DELETION_READY_SQL}) AS deletion_ready,
                           s.projection_payload,
                           (
                               SELECT interaction.result->'archive_report'
                               FROM interactions AS interaction
                               WHERE interaction.run_id = r.run_id
                                 AND interaction.status = 'completed'
                                 AND interaction.result
                                     ? 'archive_report'
                                 AND interaction.result->'archive_report'
                                     IS NOT NULL
                                 AND interaction.result->>'plan_hash'
                                     = s.plan_hash
                               ORDER BY interaction.finished_at DESC,
                                        interaction.interaction_id DESC
                               LIMIT 1
                           ),
                           EXISTS (
                               SELECT 1
                               FROM completed_layout_heads AS layout
                               WHERE layout.run_id = r.run_id
                                 AND layout.plan_hash = s.plan_hash
                           ) AS has_completed_layout,
                           COALESCE(
                               s.model_turns < s.max_model_turns
                               AND s.model_tokens < s.max_total_tokens
                               AND s.tool_calls < s.max_tool_calls
                               AND s.failures < s.max_failures,
                               false
                           ) AS interaction_budget_available,
                           acquisition.plan_hash,
                           acquisition.policy,
                           acquisition.status,
                           acquisition.approval_id,
                           acquisition.transaction_id,
                           acquisition.failure_code,
                           successor.state,
                           s.projection_payload->>'stop_reason',
                           d.folder_generation_id IS NOT NULL,
                           COALESCE(observation.retry_count, 3),
                           COALESCE(observation.status = 'active', false),
                           job.status,
                           acquisition.failure_diagnostic,
                           forward.operation_id,
                           forward.plan_hash,
                           forward.status,
                           forward.attempt_count,
                           forward.outcomes,
                           forward.items,
                           forward.warnings,
                           forward.fresh_scan_required,
                           forward.rescan_state,
                           forward.successor_run_id,
                           d.snapshot_id LIKE 'candidate-snapshot-v2:%%'
                    FROM runs AS r
                    JOIN discoveries AS d
                      ON d.discovery_id = r.discovery_id
                    JOIN config_revisions AS c
                      ON c.revision = r.config_revision
                    LEFT JOIN jobs AS job ON job.run_id = r.run_id
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
                    LEFT JOIN LATERAL (
                        SELECT p.plan_hash, p.action, p.target_relative,
                               p.file_count, p.reason_code,
                               COALESCE(
                                   fs.status,
                                   txn.status,
                                   CASE
                                       WHEN claim.approval_id IS NOT NULL
                                       THEN 'prepared'
                                       ELSE 'planned'
                                   END
                               ) AS status,
                               approval.approval_id,
                               txn.failure_code,
                               COALESCE(txn.move_backend, 'native')
                                   AS move_backend
                        FROM folder_disposition_plans AS p
                        LEFT JOIN LATERAL (
                            SELECT a.approval_id
                            FROM folder_disposition_approvals AS a
                            WHERE a.run_id = p.run_id
                              AND a.plan_hash = p.plan_hash
                            ORDER BY a.issued_at DESC, a.approval_id DESC
                            LIMIT 1
                        ) AS approval ON true
                        LEFT JOIN folder_disposition_claims AS claim
                          ON claim.approval_id = approval.approval_id
                        LEFT JOIN folder_disposition_transactions AS txn
                          ON txn.approval_id =
                             approval.approval_id
                        LEFT JOIN folder_disposition_settlements AS fs
                          ON fs.approval_id = approval.approval_id
                        WHERE p.run_id = r.run_id
                          AND (
                              p.media_plan_hash = s.plan_hash
                              OR p.media_plan_hash IS NULL
                          )
                        ORDER BY p.created_at DESC, p.plan_hash DESC
                        LIMIT 1
                    ) AS folder ON true
                    LEFT JOIN subtitle_acquisition_requests AS acquisition
                      ON acquisition.run_id = r.run_id
                    LEFT JOIN subtitle_acquisition_settlements
                        AS acquisition_settlement
                      ON acquisition_settlement.origin_run_id = r.run_id
                    LEFT JOIN subtitle_successor_outbox AS successor
                      ON successor.lineage_key =
                         acquisition_settlement.lineage_key
                    LEFT JOIN watch_folder_observations AS observation
                      ON observation.discovery_id = d.discovery_id
                    LEFT JOIN LATERAL (
                        SELECT o.operation_id, o.plan_hash, o.status,
                               o.attempt_count, o.outcomes,
                               result.items, result.warnings,
                               result.fresh_scan_required,
                               rescan.state AS rescan_state,
                               COALESCE(
                                   rescan.successor_run_id,
                                   (
                                       SELECT successor_run.run_id
                                       FROM discoveries AS successor_discovery
                                       JOIN runs AS successor_run
                                         ON successor_run.discovery_id =
                                            successor_discovery.discovery_id
                                       WHERE successor_discovery.watch_id =
                                             d.watch_id
                                         AND successor_discovery.source_folder =
                                             d.source_folder
                                         AND successor_discovery.discovered_at >
                                             d.discovered_at
                                       ORDER BY
                                           successor_discovery.discovered_at,
                                           successor_discovery.discovery_id
                                       LIMIT 1
                                   )
                               ) AS successor_run_id
                        FROM execution_operations_v2 AS o
                        LEFT JOIN execution_operation_results_v2 AS result
                          ON result.operation_id = o.operation_id
                        LEFT JOIN execution_rescan_outbox_v2 AS rescan
                          ON rescan.operation_id = o.operation_id
                        WHERE o.run_id = r.run_id
                          AND o.plan_hash = s.plan_hash
                        LIMIT 1
                    ) AS forward ON true
                    WHERE r.run_id = %s
                      AND NOT EXISTS (
                          SELECT 1 FROM run_deletions AS deleted
                          WHERE deleted.run_id = r.run_id
                      )
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        if row is None:
            return None
        stored_status = str(row[1])
        phase = None if row[3] is None else str(row[3])
        plan_hash = row[10]
        recovery_approval_id = row[11]
        apply_policy = str(row[12])
        job_status = None if row[48] is None else str(row[48])
        busy = bool(row[13]) or job_status in {"pending", "running"}
        interaction_budget_available = bool(row[36])
        folder_status = None if row[28] is None else str(row[28])
        folder_action = None if row[24] is None else str(row[24])
        acquisition_plan_hash = row[37]
        acquisition_policy = None if row[38] is None else str(row[38])
        acquisition_status = None if row[39] is None else str(row[39])
        forward_status = (
            None
            if row[52] is None
            else ExecutionOperationStatus(str(row[52]))
        )
        semantic_v2 = bool(row[60])
        busy = busy or forward_status is ExecutionOperationStatus.RUNNING
        attention_state = (
            stored_status in {"running", "failed"}
            and row[4] == "stopped"
            and plan_hash is None
            and row[44] == "needs_attention"
        )
        acquisition_attention = (
            stored_status == "running"
            and acquisition_plan_hash is not None
            and acquisition_status == "blocked"
        )
        needs_attention = (
            attention_state and stored_status == "running"
        ) or acquisition_attention
        status = "needs_attention" if needs_attention else stored_status
        actions: list[str] = []
        if not busy and needs_attention:
            if interaction_budget_available and not acquisition_attention:
                actions.append("question")
            if (
                acquisition_attention
                and row[40] is not None
                and row[42]
                in {
                    "destination_collision",
                    "atomic_move_unsupported",
                }
            ):
                actions.append("retry_subtitle_acquisition")
            if acquisition_attention:
                actions.append("fail_subtitle_acquisition")
            if bool(row[45]) and bool(row[47]) and row[48] == "completed":
                if not acquisition_attention:
                    if int(row[46]) < 3:
                        actions.append("retry_run")
                    actions.append("fail_run")
        elif (
            not busy
            and attention_state
            and bool(row[45])
            and bool(row[47])
            and row[48] == "completed"
            and row[23] is None
        ):
            actions.append("fail_run")
        if not busy and plan_hash is not None and semantic_v2:
            actions.extend(
                item.value
                for item in forward_available_actions(
                    policy=ApplyPolicy(apply_policy),
                    operation_status=forward_status,
                )
            )
        if not busy and plan_hash is not None and not semantic_v2:
            if interaction_budget_available and status in {
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
                if interaction_budget_available:
                    actions.append("revision")
                if apply_policy != "plan_only":
                    actions.append("approve_apply")
            if (
                interaction_budget_available
                and status == "completed"
                and bool(row[35])
            ):
                actions.append("reapply")
            if recovery_approval_id is not None:
                actions.append("recover")
        if (
            not semantic_v2
            and not busy
            and row[23] is not None
            and folder_status == "planned"
            and apply_policy != "plan_only"
            and (
                folder_action == "fail"
                or (row[14] is not None and row[17] == "completed")
            )
        ):
            actions.append(
                "dispose_failed_folder"
                if folder_action == "fail"
                else "settle_folder"
            )
        if (
            not semantic_v2
            and folder_status
            in {"prepared", "renamed", "recovery_required"}
            and row[29] is not None
        ):
            actions.append("recover_folder_disposition")
        if (
            not busy
            and acquisition_plan_hash is not None
            and acquisition_policy == "manual"
            and acquisition_status in {"planned", "approved"}
        ):
            actions.append("approve_subtitle_acquisition")
        if bool(row[32]):
            actions.append("delete_run")
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
            "recovery_approval_id": (
                None if semantic_v2 else recovery_approval_id
            ),
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
            "execution": (
                None
                if row[50] is None
                else self._forward_execution_view(row)
            ),
            "source_folder": (
                None if row[22] is None else str(row[22])
            ),
            "folder_disposition": (
                None
                if row[23] is None
                else {
                    "plan_hash": str(row[23]),
                    "action": str(row[24]),
                    "target_relative": (
                        None if row[25] is None else str(row[25])
                    ),
                    "file_count": int(row[26]),
                    "reason_code": str(row[27]),
                    "status": str(row[28]),
                    "recovery_approval_id": (
                        None if row[29] is None else str(row[29])
                    ),
                    "failure_code": (
                        None if row[30] is None else str(row[30])
                    ),
                    "move_backend": str(row[31]),
                }
            ),
            "archive_report": (
                row[34]
                if isinstance(row[34], dict)
                else archive_report_from_projection(row[33])
            ),
            "subtitle_acquisition": (
                None
                if acquisition_plan_hash is None
                else {
                    "plan_hash": str(acquisition_plan_hash),
                    "policy": acquisition_policy,
                    "status": acquisition_status,
                    "approval_id": (
                        None if row[40] is None else str(row[40])
                    ),
                    "transaction_id": (
                        None if row[41] is None else str(row[41])
                    ),
                    "failure_code": (
                        None if row[42] is None else str(row[42])
                    ),
                    "failure_diagnostic": (
                        row[49] if isinstance(row[49], dict) else None
                    ),
                    "successor_status": (
                        None if row[43] is None else str(row[43])
                    ),
                }
            ),
        }

    @staticmethod
    def _forward_execution_view(
        row: tuple[object, ...],
    ) -> dict[str, object]:
        outcomes = tuple(str(item) for item in (row[54] or ()))
        counts = {
            name: outcomes.count(name)
            for name in (
                "satisfied",
                "stale",
                "collision",
                "unsafe",
                "unavailable",
            )
        }
        return {
            "operation_id": str(row[50]),
            "plan_hash": str(row[51]),
            "status": str(row[52]),
            "attempt_count": int(row[53]),
            "counts": counts,
            "items": list(row[55] or ()),
            "warnings": list(row[56] or ()),
            "fresh_scan_required": bool(row[57]),
            "rescan_state": None if row[58] is None else str(row[58]),
            "successor_run_id": (
                None if row[59] is None else str(row[59])
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
                    f"""
                    WITH page_cursor AS (
                        SELECT created_at, run_id
                        FROM runs
                        WHERE run_id = %s
                    )
                    SELECT r.run_id,
                           CASE
                               WHEN r.status = 'running'
                                AND s.runtime_status = 'stopped'
                                AND s.plan_hash IS NULL
                                AND s.projection_payload->>'stop_reason'
                                    = 'needs_attention'
                               THEN 'needs_attention'
                               ELSE r.status
                           END,
                           r.work_type, r.created_at,
                           s.phase, s.plan_hash, d.source_folder,
                           ({RUN_DELETION_READY_SQL}) AS deletion_ready
                    FROM runs AS r
                    JOIN discoveries AS d
                      ON d.discovery_id = r.discovery_id
                    LEFT JOIN run_states AS s ON s.run_id = r.run_id
                    WHERE (
                        %s::text IS NULL
                        OR (r.created_at, r.run_id) < (
                            SELECT created_at, run_id FROM page_cursor
                        )
                    )
                      AND NOT EXISTS (
                          SELECT 1 FROM run_deletions AS deleted
                          WHERE deleted.run_id = r.run_id
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
                "source_folder": (
                    None if row[6] is None else str(row[6])
                ),
                "available_actions": (
                    ["delete_run"] if bool(row[7]) else []
                ),
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
                           d.discovered_at, r.run_id, r.status,
                           d.source_folder
                    FROM discoveries AS d
                    LEFT JOIN runs AS r
                      ON r.discovery_id = d.discovery_id
                     AND NOT EXISTS (
                         SELECT 1 FROM run_deletions AS deleted
                         WHERE deleted.run_id = r.run_id
                     )
                    WHERE (
                        %s::text IS NULL
                        OR (d.discovered_at, d.discovery_id) < (
                            SELECT discovered_at, discovery_id
                            FROM page_cursor
                        )
                    )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM runs AS hidden_run
                          JOIN run_deletions AS deleted
                            ON deleted.run_id = hidden_run.run_id
                          WHERE hidden_run.discovery_id = d.discovery_id
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
                "source_folder": (
                    None if row[6] is None else str(row[6])
                ),
            }
            for row in rows
        )

    def list_folder_observations(
        self, *, limit: int
    ) -> tuple[dict[str, object], ...]:
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT o.watch_id, o.folder_name, o.status,
                           o.blocked_reason, o.stable_at, r.run_id,
                           o.retry_count
                    FROM watch_folder_observations AS o
                    LEFT JOIN runs AS r
                      ON r.discovery_id = o.discovery_id
                     AND NOT EXISTS (
                         SELECT 1 FROM run_deletions AS deleted
                         WHERE deleted.run_id = r.run_id
                     )
                    ORDER BY o.first_observed_at DESC,
                             o.watch_id, o.folder_name
                    LIMIT %s
                    """,
                    (limit,),
                ).fetchall()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        return tuple(
            {
                "watch_id": str(row[0]),
                "source_folder": str(row[1]),
                "status": str(row[2]),
                "reason_code": None if row[3] is None else str(row[3]),
                "stable_at": (
                    None if row[4] is None else row[4].isoformat()
                ),
                "run_id": None if row[5] is None else str(row[5]),
                "retry_count": int(row[6]),
            }
            for row in rows
        )

    def is_run_visible(self, run_id: str) -> bool:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM runs AS run
                        WHERE run.run_id = %s
                          AND NOT EXISTS (
                              SELECT 1
                              FROM run_deletions AS deleted
                              WHERE deleted.run_id = run.run_id
                          )
                    )
                    """,
                    (run_id,),
                ).fetchone()
                return bool(row[0])
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

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
                    SELECT lineage.plan_hash, lineage.plan_kind,
                           review.schema_version, review.payload
                    FROM plan_lineage AS lineage
                    LEFT JOIN plan_reviews AS review
                      ON review.run_id = lineage.run_id
                     AND review.version = lineage.version
                     AND review.plan_hash = lineage.plan_hash
                    WHERE lineage.run_id = %s
                      AND lineage.version = %s
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
        stored_review = row[3]
        if plan_kind not in {"initial", "amendment"}:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            canonical_bytes = self._plans.load(plan_hash)
            is_amendment = (
                verify_amendment_bytes(canonical_bytes, plan_hash)
                or verify_movie_amendment_bytes(
                    canonical_bytes, plan_hash
                )
            )
            if plan_kind == "initial":
                valid_kind = (
                    not is_amendment
                    and verify_initial_plan_bytes(
                        canonical_bytes, plan_hash
                    )
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
        unmapped_ids = frozenset(
            source.candidate_id
            for source in manifest.sources
            if source.candidate_id not in moved
        )
        review_candidate_ids = (
            unmapped_ids if plan_kind == "initial" else frozenset()
        )
        try:
            stored: PlanReview | None = None
            if stored_review is not None:
                if str(row[2]) != PLAN_REVIEW_SCHEMA:
                    raise ValueError
                payload = (
                    stored_review
                    if isinstance(stored_review, dict)
                    else json.loads(str(stored_review))
                )
                stored = PlanReview.from_dict(payload)
            system = (
                self._historical_plan_review(
                    run_id=run_id,
                    plan_hash=plan_hash,
                    unmapped_ids=unmapped_ids,
                )
                if plan_kind == "initial"
                else PlanReview.unavailable()
            )
            review = merge_plan_reviews(stored, system)
            agent_explained_ids = (
                frozenset(item.candidate_id for item in stored.items)
                if (
                    stored is not None
                    and stored.status.value == "agent_and_system"
                )
                else frozenset()
            )
            source_ids = frozenset(sources)
            if any(
                item.candidate_id not in source_ids
                or (
                    plan_kind == "initial"
                    and item.candidate_id not in unmapped_ids
                )
                or (
                    item.related_video_id is not None
                    and (
                        item.related_video_id not in source_ids
                        or (
                            plan_kind == "initial"
                            and item.related_video_id not in unmapped_ids
                        )
                    )
                )
                for item in review.items
            ):
                raise ValueError
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.INTERACTION_CONFLICT
            ) from None
        explanations = {
            item.candidate_id: item for item in review.items
        }
        remaining_disposition = (
            "unmapped" if plan_kind == "initial" else "unchanged"
        )
        remaining = [
            {
                "disposition": remaining_disposition,
                "candidate_id": str(source.candidate_id),
                "kind": source.kind.value,
                "source": source.relative_path.as_posix(),
                "destination": None,
                "explanation": (
                    self._review_explanation(
                        explanations.get(source.candidate_id)
                    )
                    if plan_kind == "initial"
                    else None
                ),
            }
            for source in manifest.sources
            if source.candidate_id not in moved
        ]
        move_items = [
            {
                "disposition": "move",
                "candidate_id": str(source.candidate_id),
                "kind": source.kind.value,
                "source": source.relative_path.as_posix(),
                "destination": move.destination.as_posix(),
                "explanation": None,
            }
            for move in manifest.moves
            for source in (sources[move.source_id],)
        ]
        items = (
            [*remaining, *move_items]
            if plan_kind == "initial"
            else [*move_items, *remaining]
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
            "review": {
                "status": review.status.value,
                "agent_summary": review.agent_summary,
                "advisory_only": True,
                "coverage": {
                    "total_unmapped": counts["unmapped"],
                    "agent_explained": (
                        sum(
                            candidate_id in review_candidate_ids
                            for candidate_id in agent_explained_ids
                        )
                    ),
                    "system_verified": sum(
                        item.verification
                        is PlanReviewVerification.VERIFIED
                        for item in review.items
                        if item.candidate_id in review_candidate_ids
                    ),
                    "fallback": max(
                        0,
                        counts["unmapped"]
                        - sum(
                            item.candidate_id in review_candidate_ids
                            for item in review.items
                        ),
                    ),
                },
            },
            "items": page,
            "next_after": next_after if next_after < len(indexed) else None,
        }

    @staticmethod
    def _review_explanation(
        item: PlanReviewItem | None,
    ) -> dict[str, object]:
        if item is None:
            return {
                "reason_code": PlanReviewReason.NOT_SELECTED.value,
                "agent_detail": None,
                "verification": PlanReviewVerification.FALLBACK.value,
                "season": None,
                "episode": None,
                "related_video_id": None,
            }
        return {
            "reason_code": item.reason.value,
            "agent_detail": item.agent_detail,
            "verification": item.verification.value,
            "season": item.season,
            "episode": item.episode,
            "related_video_id": (
                None
                if item.related_video_id is None
                else str(item.related_video_id)
            ),
        }

    def _historical_plan_review(
        self,
        *,
        run_id: str,
        plan_hash: str,
        unmapped_ids: frozenset[CandidateId],
    ) -> PlanReview:
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT event_type, payload
                    FROM run_events
                    WHERE run_id = %s
                      AND event_type IN (
                        'existing_inventory_observed',
                        'archive_directory_listed',
                        'mapping_rejected',
                        'plan_built'
                      )
                    ORDER BY sequence
                    """,
                    (run_id,),
                ).fetchall()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        decoded: list[tuple[str, dict[str, object]]] = []
        for event_type, encoded in rows:
            try:
                envelope = json.loads(bytes(encoded))
                payload = envelope["payload"]
                if (
                    not isinstance(payload, dict)
                    or envelope["event_type"] != str(event_type)
                ):
                    raise ValueError
            except Exception:
                return PlanReview.unavailable()
            decoded.append((str(event_type), payload))
        plan_indexes = [
            index
            for index, (event_type, payload) in enumerate(decoded)
            if event_type == "plan_built"
            and payload.get("plan_hash") == plan_hash
        ]
        if len(plan_indexes) != 1:
            return PlanReview.unavailable()
        occupied: set[tuple[int, int]] = set()
        conflicts: dict[CandidateId, set[tuple[int, int]]] = {}
        for event_type, payload in decoded[: plan_indexes[0]]:
            if event_type == "existing_inventory_observed":
                values = payload.get("occupied")
                if isinstance(values, list):
                    occupied = {
                        (item[0], item[1])
                        for item in values
                        if (
                            isinstance(item, list)
                            and len(item) == 2
                            and type(item[0]) is int
                            and type(item[1]) is int
                        )
                    }
                continue
            if event_type == "archive_directory_listed":
                values = payload.get("occupied")
                if isinstance(values, list):
                    occupied.update(
                        (item[0], item[1])
                        for item in values
                        if (
                            isinstance(item, list)
                            and len(item) == 2
                            and type(item[0]) is int
                            and type(item[1]) is int
                        )
                    )
                continue
            if event_type != "mapping_rejected":
                continue
            issue = payload.get("issue")
            if (
                not isinstance(issue, dict)
                or issue.get("code") != "inventory_conflict"
                or not isinstance(issue.get("context"), list)
            ):
                continue
            context = {
                item.get("key"): item.get("value")
                for item in issue["context"]
                if isinstance(item, dict)
            }
            try:
                candidate_id = CandidateId.parse(context["video_id"])
                season = context["season"]
                episode = context["episode"]
                if (
                    candidate_id not in unmapped_ids
                    or type(season) is not int
                    or type(episode) is not int
                    or (season, episode) not in occupied
                ):
                    continue
            except (DomainError, KeyError, TypeError, ValueError):
                continue
            conflicts.setdefault(candidate_id, set()).add(
                (season, episode)
            )
        items = tuple(
            PlanReviewItem(
                candidate_id=candidate_id,
                reason=PlanReviewReason.EXISTING_EPISODE,
                verification=PlanReviewVerification.VERIFIED,
                season=next(iter(values))[0],
                episode=next(iter(values))[1],
            )
            for candidate_id, values in sorted(
                conflicts.items(),
                key=lambda item: item[0].ordinal,
            )
            if len(values) == 1
        )
        return (
            PlanReview.system_only(items=items)
            if items
            else PlanReview.unavailable()
        )

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
