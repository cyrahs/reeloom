from __future__ import annotations

from dataclasses import replace

from reeloom.kernel.approval import ApprovalRecord
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError
from reeloom.kernel.inventory import MAX_INVENTORY_EPISODES
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.naming import SeriesIdentity
from reeloom.kernel.naming import SubtitleVariant
from reeloom.kernel.rename_plan import RenamePlan, RootBinding
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.events import (
    ApplyFailed,
    ApplyStarted,
    ApprovalRequested,
    CandidateSnapshotCreated,
    ExistingInventoryObserved,
    MappingRejected,
    MappingSubmitted,
    ModelUsageRecorded,
    MoveApplied,
    PlanApproved,
    PlanBuilt,
    RollbackCompleted,
    RunCompleted,
    RunFailed,
    RunStarted,
    RunStopped,
    RuntimeEvent,
    SeriesSelected,
    SubtitleVariantDetected,
    TmdbCandidatesObserved,
    TmdbSeasonCatalogObserved,
    ToolRejected,
    ToolRequested,
    ToolSucceeded,
)
from reeloom.runtime.state import (
    MappingValidationIssue,
    Phase,
    RunState,
    RunStatus,
    StopReason,
)


def _is_transaction_id(value: object) -> bool:
    prefix = "txn-v1-"
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and len(value) == len(prefix) + 64
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _require_running(state: RunState) -> None:
    if state.status is not RunStatus.RUNNING:
        raise RuntimeDomainError(RuntimeErrorCode.RUN_NOT_ACTIVE)


def _complete_tool_call(
    state: RunState,
    *,
    call_id: str,
    tool_name: str,
) -> frozenset[tuple[str, str]]:
    pending_call = (call_id, tool_name)
    if pending_call not in state.pending_tool_calls:
        raise RuntimeDomainError(
            RuntimeErrorCode.UNKNOWN_TOOL_CALL,
            context={"call_id": call_id, "tool_name": tool_name},
        )
    return state.pending_tool_calls - {pending_call}


def _observe_tool_call(
    state: RunState,
    *,
    call_id: str,
    tool_name: str,
) -> frozenset[tuple[str, str]]:
    call = (call_id, tool_name)
    if (
        not call_id
        or call not in state.pending_tool_calls
        or call in state.observed_tool_calls
    ):
        raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
    return state.observed_tool_calls | {call}


def reduce_event(
    state: RunState | None,
    event: RuntimeEvent,
) -> RunState:
    """Apply one event without I/O; replaying the same events gives the same state."""

    if isinstance(event, RunStarted):
        if state is not None:
            raise RuntimeDomainError(RuntimeErrorCode.RUN_ALREADY_STARTED)
        if (
            not event.run_id
            or not isinstance(event.work_type, TmdbWorkType)
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return RunState(
            run_id=event.run_id,
            phase=Phase.BOOTSTRAP,
            status=RunStatus.RUNNING,
            event_count=1,
            tool_calls=0,
            failures=0,
            pending_tool_calls=frozenset(),
            observed_tool_calls=frozenset(),
            work_type=event.work_type,
        )

    if state is None:
        raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)

    if isinstance(event, PlanApproved):
        if (
            state.status is not RunStatus.STOPPED
            or state.phase is not Phase.AWAITING_APPROVAL
            or state.stop_reason is not StopReason.AWAITING_APPROVAL
            or state.plan_hash is None
            or event.plan_hash != state.plan_hash
            or state.approval_id is not None
            or not ApprovalRecord.is_valid_id(event.approval_id)
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        return replace(
            state,
            status=RunStatus.RUNNING,
            event_count=state.event_count + 1,
            stop_reason=None,
            approval_id=event.approval_id,
        )

    _require_running(state)
    event_count = state.event_count + 1

    if isinstance(event, CandidateSnapshotCreated):
        valid_candidate_ids = (
            event.candidate_ids is None
            or (
                isinstance(event.candidate_ids, tuple)
                and len(event.candidate_ids) == event.candidate_count
                and len(set(event.candidate_ids))
                == len(event.candidate_ids)
                and all(
                    isinstance(candidate_id, CandidateId)
                    for candidate_id in event.candidate_ids
                )
            )
        )
        valid_roots = (
            event.source_root is None
            and event.output_root is None
        ) or (
            isinstance(event.source_root, RootBinding)
            and isinstance(event.output_root, RootBinding)
        )
        if (
            state.candidate_snapshot_id is not None
            or state.tool_calls != 0
            or state.pending_tool_calls
            or state.observed_tool_calls
            or state.phase is not Phase.BOOTSTRAP
            or not event.snapshot_id
            or type(event.candidate_count) is not int
            or event.candidate_count < 0
            or not valid_candidate_ids
            or not valid_roots
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        return replace(
            state,
            phase=Phase.IDENTIFY_SERIES,
            event_count=event_count,
            candidate_snapshot_id=event.snapshot_id,
            candidate_count=event.candidate_count,
            candidate_ids=event.candidate_ids,
            authorized_source_root=event.source_root,
            authorized_output_root=event.output_root,
        )

    if isinstance(event, ToolRequested):
        if not event.call_id or not event.tool_name:
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        if any(
            call_id == event.call_id
            for call_id, _ in state.pending_tool_calls
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.DUPLICATE_TOOL_CALL,
                context={"call_id": event.call_id},
            )
        return replace(
            state,
            event_count=event_count,
            tool_calls=state.tool_calls + 1,
            pending_tool_calls=state.pending_tool_calls
            | {(event.call_id, event.tool_name)},
        )

    if isinstance(event, TmdbCandidatesObserved):
        if (
            state.phase is not Phase.IDENTIFY_SERIES
            or not isinstance(event.candidates, tuple)
            or len(event.candidates) > 20
            or len(set(event.candidates)) != len(event.candidates)
            or any(
                not isinstance(candidate, TmdbCandidateRef)
                or candidate.work_type is not state.work_type
                for candidate in event.candidates
            )
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        candidates = state.tmdb_candidates | frozenset(
            event.candidates
        )
        if len(candidates) > 200:
            raise RuntimeDomainError(
                RuntimeErrorCode.TMDB_CANDIDATE_LIMIT_EXCEEDED
            )
        return replace(
            state,
            event_count=event_count,
            tmdb_candidates=candidates,
        )

    if isinstance(event, SeriesSelected):
        series = event.series
        if (
            state.phase is not Phase.IDENTIFY_SERIES
            or state.selected_series is not None
            or not isinstance(series, SeriesIdentity)
            or not isinstance(event.work_type, TmdbWorkType)
            or event.work_type is not state.work_type
            or TmdbCandidateRef(
                work_type=event.work_type,
                tmdb_id=series.tmdb_id,
            )
            not in state.tmdb_candidates
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        return replace(
            state,
            phase=Phase.MAP_EPISODES,
            event_count=event_count,
            selected_series=series,
            selected_work_type=event.work_type,
        )

    if isinstance(event, TmdbSeasonCatalogObserved):
        observed = _observe_tool_call(
            state,
            call_id=event.call_id,
            tool_name="get_tmdb_season",
        )
        if (
            state.phase is not Phase.MAP_EPISODES
            or state.selected_series is None
            or event.tmdb_id != state.selected_series.tmdb_id
            or event.work_type is not state.selected_work_type
            or type(event.season_number) is not int
            or event.season_number < 0
            or type(event.episode_count) is not int
            or event.episode_count < 1
            or event.episode_count > 100_000
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        counts = dict(state.episode_catalog_counts)
        previous = counts.get(event.season_number)
        if previous is not None and previous != event.episode_count:
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        counts[event.season_number] = event.episode_count
        if len(counts) > 100:
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return replace(
            state,
            event_count=event_count,
            episode_catalog_counts=tuple(sorted(counts.items())),
            observed_tool_calls=observed,
        )

    if isinstance(event, ExistingInventoryObserved):
        observed = _observe_tool_call(
            state,
            call_id=event.call_id,
            tool_name="get_existing_inventory",
        )
        valid_occupied = (
            isinstance(event.occupied, tuple)
            and len(event.occupied) <= MAX_INVENTORY_EPISODES
            and tuple(sorted(event.occupied)) == event.occupied
            and len(set(event.occupied)) == len(event.occupied)
            and all(
                isinstance(item, tuple)
                and len(item) == 2
                and type(item[0]) is int
                and 0 <= item[0] <= 999
                and type(item[1]) is int
                and 1 <= item[1] <= 100_000
                for item in event.occupied
            )
        )
        if (
            state.phase is not Phase.MAP_EPISODES
            or state.selected_series is None
            or event.tmdb_id != state.selected_series.tmdb_id
            or event.work_type is not state.selected_work_type
            or not valid_occupied
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return replace(
            state,
            event_count=event_count,
            inventory_episodes=event.occupied,
            observed_tool_calls=observed,
        )

    if isinstance(event, SubtitleVariantDetected):
        observed = _observe_tool_call(
            state,
            call_id=event.call_id,
            tool_name="detect_subtitle_variant",
        )
        if (
            state.phase is not Phase.MAP_EPISODES
            or not isinstance(event.subtitle_id, CandidateId)
            or event.subtitle_id.kind is not CandidateKind.SUBTITLE
            or state.candidate_ids is None
            or event.subtitle_id not in state.candidate_ids
            or not isinstance(event.variant, SubtitleVariant)
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        variants = dict(state.subtitle_variants)
        previous = variants.get(event.subtitle_id)
        if previous is not None and previous is not event.variant:
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        variants[event.subtitle_id] = event.variant
        return replace(
            state,
            event_count=event_count,
            subtitle_variants=tuple(
                sorted(
                    variants.items(),
                    key=lambda item: item[0].ordinal,
                )
            ),
            observed_tool_calls=observed,
        )

    if isinstance(event, MappingRejected):
        observed = _observe_tool_call(
            state,
            call_id=event.call_id,
            tool_name="submit_mapping",
        )
        if (
            state.phase is not Phase.MAP_EPISODES
            or not isinstance(event.issue, MappingValidationIssue)
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return replace(
            state,
            event_count=event_count,
            validation_issues=(event.issue,),
            observed_tool_calls=observed,
        )

    if isinstance(event, MappingSubmitted):
        observed = _observe_tool_call(
            state,
            call_id=event.call_id,
            tool_name="submit_mapping",
        )
        detected_subtitles = {
            subtitle_id for subtitle_id, _ in state.subtitle_variants
        }
        if (
            state.phase is not Phase.MAP_EPISODES
            or not isinstance(event.mapping, MappingDraft)
            or event.candidate_snapshot_id
            != state.candidate_snapshot_id
            or state.candidate_ids is None
            or not state.episode_catalog_counts
            or state.inventory_episodes is None
            or any(
                subtitle.subtitle_id not in detected_subtitles
                for subtitle in event.mapping.subtitles
            )
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        candidate_ids = set(state.candidate_ids)
        if any(
            video.video_id not in candidate_ids
            for video in event.mapping.videos
        ) or any(
            subtitle.subtitle_id not in candidate_ids
            or subtitle.video_id not in candidate_ids
            for subtitle in event.mapping.subtitles
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        try:
            catalog = EpisodeCatalog(
                season_episode_counts=state.episode_catalog_counts
            )
            for video in event.mapping.videos:
                catalog.validate(video.span)
        except DomainError:
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            ) from None
        occupied = set(state.inventory_episodes)
        if any(
            (video.span.season, episode) in occupied
            for video in event.mapping.videos
            for episode in video.span.episodes
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        return replace(
            state,
            phase=Phase.BUILD_PLAN,
            event_count=event_count,
            mapping_draft=event.mapping,
            validation_issues=(),
            observed_tool_calls=observed,
        )

    if isinstance(event, PlanBuilt):
        plan = event.plan
        if (
            state.phase is not Phase.BUILD_PLAN
            or state.rename_plan is not None
            or state.plan_hash is not None
            or state.pending_tool_calls
            or not isinstance(plan, RenamePlan)
            or not plan.verify_hash()
            or plan.run_id != state.run_id
            or plan.work_type is not state.work_type
            or plan.candidate_snapshot_id
            != state.candidate_snapshot_id
            or plan.draft.series != state.selected_series
            or plan.draft.mapping != state.mapping_draft
            or plan.source_root != state.authorized_source_root
            or plan.output_root != state.authorized_output_root
            or state.candidate_ids is None
            or {
                source.candidate_id for source in plan.sources
            }
            != set(state.candidate_ids)
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        return replace(
            state,
            event_count=event_count,
            rename_plan=plan,
            plan_hash=plan.plan_hash,
        )

    if isinstance(event, ApprovalRequested):
        if (
            state.phase is not Phase.BUILD_PLAN
            or state.rename_plan is None
            or state.plan_hash is None
            or event.plan_hash != state.plan_hash
            or not state.rename_plan.verify_hash()
            or state.pending_tool_calls
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        return replace(
            state,
            phase=Phase.AWAITING_APPROVAL,
            event_count=event_count,
        )

    if isinstance(event, ApplyStarted):
        if (
            state.phase is not Phase.AWAITING_APPROVAL
            or state.plan_hash is None
            or event.plan_hash != state.plan_hash
            or state.approval_id is None
            or event.approval_id != state.approval_id
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        return replace(
            state,
            phase=Phase.APPLYING,
            event_count=event_count,
        )

    if isinstance(event, MoveApplied):
        expected = (
            {
                move.source_id
                for move in state.rename_plan.draft.moves
            }
            if state.rename_plan is not None
            else set()
        )
        if (
            state.phase is not Phase.APPLYING
            or event.source_id not in expected
            or event.source_id in state.applied_source_ids
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return replace(
            state,
            event_count=event_count,
            applied_source_ids=(
                *state.applied_source_ids,
                event.source_id,
            ),
        )

    if isinstance(event, ApplyFailed):
        if (
            state.phase is not Phase.APPLYING
            or state.failure_code is not None
            or not event.code
            or len(event.code.encode("utf-8")) > 80
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return replace(
            state,
            event_count=event_count,
            failures=state.failures + 1,
            failure_code=event.code,
        )

    if isinstance(event, RollbackCompleted):
        if (
            state.phase is not Phase.APPLYING
            or state.failure_code is None
            or not _is_transaction_id(event.transaction_id)
            or type(event.rolled_back_count) is not int
            or event.rolled_back_count < 0
            or state.rename_plan is None
            or event.rolled_back_count > len(
                state.rename_plan.draft.moves
            )
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        return replace(
            state,
            phase=Phase.ROLLED_BACK,
            status=RunStatus.STOPPED,
            event_count=event_count,
            transaction_id=event.transaction_id,
            rolled_back_count=event.rolled_back_count,
        )

    if isinstance(event, RunCompleted):
        expected_sources = (
            tuple(
                move.source_id
                for move in state.rename_plan.draft.moves
            )
            if state.rename_plan is not None
            else ()
        )
        if (
            state.phase is not Phase.APPLYING
            or not _is_transaction_id(event.transaction_id)
            or type(event.applied_count) is not int
            or event.applied_count != len(expected_sources)
            or state.applied_source_ids != expected_sources
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        return replace(
            state,
            phase=Phase.COMPLETED,
            status=RunStatus.STOPPED,
            event_count=event_count,
            transaction_id=event.transaction_id,
            applied_count=event.applied_count,
            failure_code=None,
        )

    if isinstance(event, ModelUsageRecorded):
        if (
            type(event.input_tokens) is not int
            or event.input_tokens < 0
            or type(event.output_tokens) is not int
            or event.output_tokens < 0
            or type(event.total_tokens) is not int
            or event.total_tokens < 0
            or event.total_tokens
            != event.input_tokens + event.output_tokens
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return replace(
            state,
            event_count=event_count,
            model_turns=state.model_turns + 1,
            model_tokens=state.model_tokens + event.total_tokens,
        )

    if isinstance(event, ToolSucceeded):
        pending = _complete_tool_call(
            state,
            call_id=event.call_id,
            tool_name=event.tool_name,
        )
        return replace(
            state,
            event_count=event_count,
            pending_tool_calls=pending,
            observed_tool_calls=state.observed_tool_calls
            - {(event.call_id, event.tool_name)},
        )

    if isinstance(event, ToolRejected):
        pending = _complete_tool_call(
            state,
            call_id=event.call_id,
            tool_name=event.tool_name,
        )
        return replace(
            state,
            event_count=event_count,
            failures=state.failures + 1,
            pending_tool_calls=pending,
            observed_tool_calls=state.observed_tool_calls
            - {(event.call_id, event.tool_name)},
        )

    if isinstance(event, RunStopped):
        if (
            state.pending_tool_calls
            and event.reason is not StopReason.BUDGET_EXHAUSTED
        ) or (
            event.reason is StopReason.AWAITING_APPROVAL
            and state.phase is not Phase.AWAITING_APPROVAL
        ) or (
            state.phase is Phase.AWAITING_APPROVAL
            and event.reason is not StopReason.AWAITING_APPROVAL
        ) or (
            state.phase is Phase.BUILD_PLAN
            and event.reason is not StopReason.BUDGET_EXHAUSTED
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        return replace(
            state,
            status=RunStatus.STOPPED,
            event_count=event_count,
            pending_tool_calls=frozenset(),
            observed_tool_calls=frozenset(),
            stop_reason=event.reason,
        )

    if isinstance(event, RunFailed):
        if not event.code:
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return replace(
            state,
            phase=Phase.FAILED,
            status=RunStatus.FAILED,
            event_count=event_count,
            pending_tool_calls=frozenset(),
            observed_tool_calls=frozenset(),
            stop_reason=StopReason.FATAL_ERROR,
            failure_code=event.code,
        )

    raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
