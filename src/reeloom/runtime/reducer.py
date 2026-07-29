from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from reeloom.kernel.approval import ApprovalRecord
from reeloom.kernel.archive_directory import (
    ArchiveDirectoryCapability,
    ArchiveDirectoryListing,
    ArchiveSearchRecord,
)
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError
from reeloom.kernel.inventory import MAX_INVENTORY_EPISODES
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.movie import MovieMappingDraft
from reeloom.kernel.plan_review import PlanReview
from reeloom.kernel.movie_plan import MovieRenamePlan
from reeloom.kernel.naming import MovieIdentity, SeriesIdentity
from reeloom.kernel.naming import SubtitleVariant
from reeloom.kernel.rename_plan import RenamePlan, RootBinding
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.events import (
    ApplyFailed,
    ApplyStarted,
    ArchiveDirectoryListed,
    ArchiveSearchObserved,
    ApprovalRequested,
    CandidateSnapshotCreated,
    ExistingInventoryObserved,
    ExecutionSettled,
    InteractionCompleted,
    MappingRejected,
    MappingReviewCaptured,
    MappingSubmitted,
    MovieMappingSubmitted,
    MovieSelected,
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


def _is_plan_hash(value: object) -> bool:
    prefix = "sha256:"
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and len(value) == len(prefix) + 64
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def reduce_interaction_head(
    *,
    phase: Phase,
    plan_hash: str | None,
    event: InteractionCompleted,
) -> tuple[Phase, str]:
    if not _is_plan_hash(plan_hash) or not _is_plan_hash(
        event.final_plan_hash
    ):
        raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
    if event.kind == "question":
        if (
            event.fresh_mapping_submitted
            or event.plan_hash is not None
            or event.final_plan_hash != plan_hash
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        return phase, plan_hash
    if not event.fresh_mapping_submitted:
        raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
    if event.kind == "revision":
        if (
            phase is not Phase.AWAITING_APPROVAL
            or not _is_plan_hash(event.plan_hash)
            or event.plan_hash == plan_hash
            or event.final_plan_hash != event.plan_hash
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        return Phase.AWAITING_APPROVAL, event.final_plan_hash
    if event.kind != "reapply" or phase not in {
        Phase.COMPLETED,
        Phase.AWAITING_APPROVAL,
    }:
        raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
    if event.plan_hash is not None:
        if (
            not _is_plan_hash(event.plan_hash)
            or event.plan_hash == plan_hash
            or event.final_plan_hash != event.plan_hash
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        return Phase.AWAITING_APPROVAL, event.final_plan_hash
    if (
        phase is Phase.COMPLETED
        and event.final_plan_hash != plan_hash
    ) or (
        phase is Phase.AWAITING_APPROVAL
        and event.final_plan_hash == plan_hash
    ):
        raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
    return Phase.COMPLETED, event.final_plan_hash


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
            or not isinstance(event.budget, RunBudget)
            or not isinstance(event.deadline_at, datetime)
            or event.deadline_at.tzinfo is None
            or event.deadline_at.utcoffset() is None
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
            budget=event.budget,
            deadline_at=event.deadline_at,
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

    if isinstance(event, InteractionCompleted):
        valid_common = (
            state.status is RunStatus.STOPPED
            and isinstance(event.interaction_id, str)
            and bool(event.interaction_id)
            and len(event.interaction_id.encode("utf-8")) <= 128
            and event.kind in {"question", "revision", "reapply"}
            and type(event.model_turns) is int
            and event.model_turns >= 1
            and type(event.model_tokens) is int
            and event.model_tokens >= 0
            and type(event.tool_calls) is int
            and event.tool_calls >= 0
            and type(event.failures) is int
            and event.failures >= 0
            and state.model_turns + event.model_turns
            <= state.budget.max_model_turns
            and state.model_tokens + event.model_tokens
            <= state.budget.max_total_tokens
            and state.tool_calls + event.tool_calls
            <= state.budget.max_tool_calls
            and state.failures + event.failures
            <= state.budget.max_failures
        )
        if not valid_common:
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        next_phase, final_plan_hash = reduce_interaction_head(
            phase=state.phase,
            plan_hash=state.plan_hash,
            event=event,
        )
        return replace(
            state,
            phase=next_phase,
            status=RunStatus.STOPPED,
            event_count=state.event_count + 1,
            model_turns=state.model_turns + event.model_turns,
            model_tokens=state.model_tokens + event.model_tokens,
            tool_calls=state.tool_calls + event.tool_calls,
            failures=state.failures + event.failures,
            plan_hash=final_plan_hash,
            stop_reason=(
                state.stop_reason
                if event.kind == "question"
                else (
                    StopReason.AWAITING_APPROVAL
                    if next_phase is Phase.AWAITING_APPROVAL
                    else None
                )
            ),
        )

    if isinstance(event, ExecutionSettled):
        if (
            state.status is not RunStatus.STOPPED
            or state.phase is not Phase.AWAITING_APPROVAL
            or event.plan_hash != state.plan_hash
            or not ApprovalRecord.is_valid_id(event.approval_id)
            or not _is_transaction_id(event.transaction_id)
            or event.status not in {"completed", "rolled_back"}
            or type(event.applied_count) is not int
            or event.applied_count < 0
            or type(event.rolled_back_count) is not int
            or event.rolled_back_count < 0
            or (
                event.status == "completed"
                and (
                    event.rolled_back_count != 0
                    or event.failure_code is not None
                )
            )
            or (
                event.status == "rolled_back"
                and event.rolled_back_count > event.applied_count
                and event.applied_count != 0
            )
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        return replace(
            state,
            phase=(
                Phase.COMPLETED
                if event.status == "completed"
                else Phase.ROLLED_BACK
            ),
            status=RunStatus.STOPPED,
            event_count=state.event_count + 1,
            approval_id=event.approval_id,
            transaction_id=event.transaction_id,
            applied_count=event.applied_count,
            rolled_back_count=event.rolled_back_count,
            failure_code=event.failure_code,
            stop_reason=None,
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
            phase=(
                Phase.IDENTIFY_MOVIE
                if state.work_type is TmdbWorkType.MOVIE
                else Phase.IDENTIFY_SERIES
            ),
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
            state.phase
            not in {Phase.IDENTIFY_SERIES, Phase.IDENTIFY_MOVIE}
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

    if isinstance(event, MovieSelected):
        movie = event.movie
        if (
            state.phase is not Phase.IDENTIFY_MOVIE
            or state.selected_movie is not None
            or not isinstance(movie, MovieIdentity)
            or event.work_type is not TmdbWorkType.MOVIE
            or event.work_type is not state.work_type
            or TmdbCandidateRef(
                work_type=event.work_type,
                tmdb_id=movie.tmdb_id,
            )
            not in state.tmdb_candidates
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        return replace(
            state,
            phase=Phase.MAP_MOVIE,
            event_count=event_count,
            selected_movie=movie,
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

    if isinstance(event, ArchiveSearchObserved):
        observed = _observe_tool_call(
            state,
            call_id=event.search.call_id,
            tool_name="search_dir",
        )
        identity = state.selected_movie or state.selected_series
        previous_search = next(
            (
                item
                for item in reversed(state.archive_searches)
                if item.mode == event.search.mode
                and item.query == event.search.query
                and item.tmdb_id == event.search.tmdb_id
                and item.work_type is event.search.work_type
            ),
            None,
        )
        if (
            state.phase not in {Phase.MAP_EPISODES, Phase.MAP_MOVIE}
            or identity is None
            or not isinstance(event.search, ArchiveSearchRecord)
            or event.search.tmdb_id != identity.tmdb_id
            or event.search.work_type is not state.work_type
            or (
                event.search.cursor != 0
                and (
                    previous_search is None
                    or previous_search.next_cursor
                    != event.search.cursor
                )
            )
            or not isinstance(event.capabilities, tuple)
            or tuple(
                item.directory_id for item in event.capabilities
            )
            != event.search.directory_ids
            or any(
                not isinstance(item, ArchiveDirectoryCapability)
                or item.run_id != state.run_id
                or item.parent_id is not None
                or item.depth != 1
                for item in event.capabilities
            )
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        capabilities = {
            item.directory_id: item
            for item in state.archive_directory_capabilities
        }
        paths = {
            item.relative_path: item.directory_id
            for item in state.archive_directory_capabilities
        }
        for item in event.capabilities:
            previous = capabilities.get(item.directory_id)
            path_owner = paths.get(item.relative_path)
            if (
                previous is not None
                and previous != item
                or path_owner is not None
                and path_owner != item.directory_id
            ):
                raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
            capabilities[item.directory_id] = item
            paths[item.relative_path] = item.directory_id
        if len(capabilities) > 256 or len(state.archive_searches) >= 100:
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return replace(
            state,
            event_count=event_count,
            archive_directory_capabilities=tuple(
                sorted(
                    capabilities.values(),
                    key=lambda item: item.directory_id,
                )
            ),
            archive_searches=state.archive_searches + (event.search,),
            inventory_episodes=(
                ()
                if state.phase is Phase.MAP_EPISODES
                and state.inventory_episodes is None
                else state.inventory_episodes
            ),
            observed_tool_calls=observed,
        )

    if isinstance(event, ArchiveDirectoryListed):
        observed = _observe_tool_call(
            state,
            call_id=event.listing.call_id,
            tool_name="list_dir",
        )
        capabilities = {
            item.directory_id: item
            for item in state.archive_directory_capabilities
        }
        parent = capabilities.get(event.listing.directory_id)
        previous_listing = next(
            (
                item
                for item in reversed(
                    state.archive_directory_listings
                )
                if item.directory_id == event.listing.directory_id
            ),
            None,
        )
        if (
            state.phase not in {Phase.MAP_EPISODES, Phase.MAP_MOVIE}
            or not isinstance(event.listing, ArchiveDirectoryListing)
            or parent is None
            or (
                event.listing.cursor != 0
                and (
                    previous_listing is None
                    or previous_listing.next_cursor
                    != event.listing.cursor
                )
            )
            or tuple(
                item.directory_id for item in event.capabilities
            )
            != event.listing.child_ids
            or any(
                not isinstance(item, ArchiveDirectoryCapability)
                or item.run_id != state.run_id
                or item.parent_id != parent.directory_id
                or item.depth != parent.depth + 1
                or item.relative_path.parent != parent.relative_path
                for item in event.capabilities
            )
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        paths = {
            item.relative_path: item.directory_id
            for item in capabilities.values()
        }
        for item in event.capabilities:
            previous = capabilities.get(item.directory_id)
            path_owner = paths.get(item.relative_path)
            if (
                previous is not None
                and previous != item
                or path_owner is not None
                and path_owner != item.directory_id
            ):
                raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
            capabilities[item.directory_id] = item
            paths[item.relative_path] = item.directory_id
        listings = state.archive_directory_listings + (event.listing,)
        if (
            len(capabilities) > 256
            or len(listings) > 256
            or sum(len(item.videos) for item in listings) > 2_000
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        inventory = set(state.inventory_episodes or ())
        inventory.update(event.listing.occupied)
        return replace(
            state,
            event_count=event_count,
            archive_directory_capabilities=tuple(
                sorted(
                    capabilities.values(),
                    key=lambda item: item.directory_id,
                )
            ),
            archive_directory_listings=listings,
            inventory_episodes=(
                tuple(sorted(inventory))
                if state.phase is Phase.MAP_EPISODES
                else state.inventory_episodes
            ),
            observed_tool_calls=observed,
        )

    if isinstance(event, SubtitleVariantDetected):
        observed = _observe_tool_call(
            state,
            call_id=event.call_id,
            tool_name="detect_subtitle_variant",
        )
        if (
            state.phase not in {Phase.MAP_EPISODES, Phase.MAP_MOVIE}
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
            state.phase not in {Phase.MAP_EPISODES, Phase.MAP_MOVIE}
            or not isinstance(event.issue, MappingValidationIssue)
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return replace(
            state,
            event_count=event_count,
            validation_issues=(event.issue,),
            mapping_review=None,
            mapping_review_call_id=None,
            mapping_conflicts=(
                state.mapping_conflicts
                if (
                    event.issue.code != "inventory_conflict"
                    or event.issue in state.mapping_conflicts
                    or len(state.mapping_conflicts) >= 128
                )
                else (*state.mapping_conflicts, event.issue)
            ),
            observed_tool_calls=observed,
        )

    if isinstance(event, MappingReviewCaptured):
        if (
            state.phase not in {Phase.MAP_EPISODES, Phase.MAP_MOVIE}
            or not isinstance(event.review, PlanReview)
            or not isinstance(event.call_id, str)
            or not event.call_id
            or (event.call_id, "submit_mapping")
            not in state.pending_tool_calls
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return replace(
            state,
            event_count=event_count,
            mapping_review=event.review,
            mapping_review_call_id=event.call_id,
        )

    if isinstance(event, MovieMappingSubmitted):
        observed = _observe_tool_call(
            state,
            call_id=event.call_id,
            tool_name="submit_mapping",
        )
        detected = {
            candidate_id for candidate_id, _ in state.subtitle_variants
        }
        if (
            state.phase is not Phase.MAP_MOVIE
            or state.selected_movie is None
            or not isinstance(event.mapping, MovieMappingDraft)
            or event.candidate_snapshot_id != state.candidate_snapshot_id
            or state.candidate_ids is None
            or event.mapping.video_id not in state.candidate_ids
            or state.mapping_review_call_id
            not in {None, event.call_id}
            or any(
                item not in state.candidate_ids or item not in detected
                for item in event.mapping.subtitle_ids
            )
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        return replace(
            state,
            phase=Phase.BUILD_PLAN,
            event_count=event_count,
            movie_mapping_draft=event.mapping,
            validation_issues=(),
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
            or state.mapping_review_call_id
            not in {None, event.call_id}
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
        movie_plan = isinstance(plan, MovieRenamePlan)
        plan_matches_domain = (
            (
                movie_plan
                and state.work_type is TmdbWorkType.MOVIE
                and plan.draft.movie == state.selected_movie
                and plan.draft.mapping == state.movie_mapping_draft
            )
            or (
                isinstance(plan, RenamePlan)
                and state.work_type is not TmdbWorkType.MOVIE
                and plan.draft.series == state.selected_series
                and plan.draft.mapping == state.mapping_draft
            )
        )
        if (
            state.phase is not Phase.BUILD_PLAN
            or state.rename_plan is not None
            or state.plan_hash is not None
            or state.pending_tool_calls
            or not isinstance(plan, (RenamePlan, MovieRenamePlan))
            or not plan.verify_hash()
            or plan.run_id != state.run_id
            or plan.work_type is not state.work_type
            or plan.candidate_snapshot_id
            != state.candidate_snapshot_id
            or not plan_matches_domain
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
            retryable_directory_failure=(
                False
                if event.tool_name in {"search_dir", "list_dir"}
                else state.retryable_directory_failure
            ),
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
            retryable_directory_failure=(
                event.retryable and event.code.startswith("directory_")
                if event.tool_name in {"search_dir", "list_dir"}
                else state.retryable_directory_failure
            ),
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
