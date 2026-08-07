from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reeloom.server.config import (
    MAX_FAILURES,
    MAX_MODEL_TURNS,
    MAX_TOOL_CALLS,
    MAX_TOTAL_TOKENS,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ErrorBody(_StrictModel):
    code: str


class ErrorResponse(_StrictModel):
    error: ErrorBody


class SessionResponse(_StrictModel):
    api_version: Literal["1.0.0"]
    role: Literal["admin"]


class HealthResponse(_StrictModel):
    status: Literal["ok"]
    postgres_major: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    notification_pending: int = Field(default=0, ge=0)
    notification_dead: int = Field(default=0, ge=0)
    telegram_configured: bool = False


class DirectoryItem(_StrictModel):
    name: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=4_096)


class DirectoryListingResponse(_StrictModel):
    path: str = Field(max_length=4_096)
    absolute_path: str = Field(min_length=1, max_length=4_096)
    parent: str | None = Field(max_length=4_096)
    directories: list[DirectoryItem] = Field(max_length=1_000)


class RunSummary(_StrictModel):
    run_id: str
    status: str
    work_type: Literal["anime", "tv", "movie"]
    created_at: str
    phase: str | None
    plan_hash: str | None
    source_folder: str | None = None
    available_actions: list[Literal["delete_run"]]


class RunsResponse(_StrictModel):
    items: list[RunSummary]


class DiscoverySummary(_StrictModel):
    discovery_id: str
    watch_id: str
    work_type: Literal["anime", "tv", "movie"]
    discovered_at: str
    run_id: str | None
    run_status: str | None
    source_folder: str | None = None


class DiscoveriesResponse(_StrictModel):
    items: list[DiscoverySummary]


class FolderObservationSummary(_StrictModel):
    watch_id: str
    source_folder: str
    status: Literal["settling", "active", "blocked", "settled"]
    reason_code: str | None
    stable_at: str | None
    run_id: str | None
    retry_count: int = Field(ge=0, le=3)


class FolderObservationsResponse(_StrictModel):
    items: list[FolderObservationSummary]


class RunSettlement(_StrictModel):
    approval_id: str
    plan_hash: str
    transaction_id: str
    status: Literal["completed", "rolled_back"]
    applied_count: int = Field(ge=0)
    rolled_back_count: int = Field(ge=0)
    failure_code: str | None
    settled_at: str


class ForwardExecutionCounts(_StrictModel):
    satisfied: int = Field(ge=0)
    stale: int = Field(ge=0)
    collision: int = Field(ge=0)
    unsafe: int = Field(ge=0)
    unavailable: int = Field(ge=0)


class ForwardExecutionItemView(_StrictModel):
    source_id: str
    outcome: Literal[
        "satisfied", "stale", "collision", "unsafe", "unavailable"
    ]
    diagnostic: Literal[
        "native",
        "checked_rename",
        "collision",
        "cross_filesystem",
        "permission_denied",
        "transient_io",
        "unsafe",
        "unknown",
    ] | None


class ForwardExecutionView(_StrictModel):
    operation_id: str
    plan_hash: str
    status: Literal[
        "authorized",
        "running",
        "completed",
        "partial",
        "stale",
        "collision",
        "unsafe",
        "unavailable",
        "superseded",
    ]
    attempt_count: int = Field(ge=0, le=100)
    counts: ForwardExecutionCounts
    items: list[ForwardExecutionItemView] = Field(max_length=10_000)
    warnings: list[str] = Field(max_length=1_000)
    fresh_scan_required: bool
    rescan_state: Literal[
        "queued", "leased", "retry_wait", "completed", "blocked"
    ] | None
    successor_run_id: str | None


class FolderDispositionView(_StrictModel):
    plan_hash: str
    action: Literal["archive", "fail", "remove_empty"]
    target_relative: str | None
    file_count: int = Field(ge=0)
    reason_code: str
    status: Literal[
        "planned",
        "prepared",
        "renamed",
        "completed",
        "blocked",
        "recovery_required",
    ]
    recovery_approval_id: str | None
    failure_code: str | None = None
    move_backend: Literal["native", "clouddrive_webdav"] = "native"


class ArchiveReportSearch(_StrictModel):
    mode: Literal["selected_tmdb_id", "name"]
    match_count: int = Field(ge=0, le=50)
    complete: bool


class ArchiveReportEntry(_StrictModel):
    entry_id: int = Field(ge=1)
    parent_entry_id: int | None = Field(ge=1)
    kind: Literal["directory", "video"]
    name: str = Field(min_length=1, max_length=255)
    depth: int = Field(ge=1, le=4)
    listed: bool


class ArchiveReport(_StrictModel):
    status: Literal["checked", "incomplete"]
    work_type: Literal["anime", "tv", "movie"]
    tmdb_id: int = Field(ge=1, le=9_999_999_999)
    searches: list[ArchiveReportSearch] = Field(max_length=100)
    entries: list[ArchiveReportEntry] = Field(max_length=200)
    possible_existing_archive: bool
    advisory_only: Literal[True]
    observed_at: str


class RunResponse(_StrictModel):
    run_id: str
    status: str
    work_type: Literal["anime", "tv", "movie"]
    phase: str | None
    runtime_status: str | None
    event_sequence: int = Field(ge=0)
    model_turns: int = Field(ge=0)
    model_tokens: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    failures: int = Field(ge=0)
    plan_hash: str | None
    recovery_approval_id: str | None
    apply_policy: Literal["plan_only", "manual", "automatic"]
    available_actions: list[
        Literal[
            "question",
            "revision",
            "approve_apply",
            "execute",
            "reapply",
            "recover",
            "settle_folder",
            "dispose_failed_folder",
            "recover_folder_disposition",
            "approve_subtitle_acquisition",
            "retry_subtitle_acquisition",
            "fail_subtitle_acquisition",
            "retry_run",
            "fail_run",
            "delete_run",
        ]
    ]
    settlement: RunSettlement | None
    execution: ForwardExecutionView | None = None
    source_folder: str | None = None
    folder_disposition: FolderDispositionView | None = None
    archive_report: ArchiveReport | None
    subtitle_acquisition: SubtitleAcquisitionView | None = None


class SubtitleFailureDiagnostic(_StrictModel):
    schema_version: Literal[1]
    stage: Literal[
        "destination_preflight",
        "staging_prepare",
        "staging_validate",
        "member_write",
        "publish",
    ]
    reason: Literal[
        "name_exists",
        "create_failed",
        "entry_type_mismatch",
        "unsafe_permissions",
        "owner_mismatch",
        "not_empty",
        "unexpected_entries",
        "casefold_collision",
    ]
    actual_mode: int | None = Field(default=None, ge=0, le=0o777)
    actual_uid: int | None = Field(default=None, ge=0)
    entry_count: int | None = Field(default=None, ge=0, le=256)
    expected_policy: Literal[
        "owner_rwx_no_group_or_other_write"
    ] | None = None
    expected_uid: int | None = Field(default=None, ge=0)
    member_index: int | None = Field(default=None, ge=0, le=255)


class SubtitleAcquisitionView(_StrictModel):
    plan_hash: str
    policy: Literal["plan_only", "manual", "automatic"]
    status: Literal["planned", "approved", "published", "blocked"]
    approval_id: str | None
    transaction_id: str | None
    failure_code: str | None
    failure_diagnostic: SubtitleFailureDiagnostic | None = None
    successor_status: Literal[
        "queued", "retry_wait", "leased", "completed", "blocked"
    ] | None = None


class PlanLineageItem(_StrictModel):
    run_id: str
    version: int = Field(ge=1)
    plan_hash: str
    parent_plan_hash: str | None
    plan_kind: Literal["initial", "amendment"]
    created_at: str


class PlanLineageResponse(_StrictModel):
    items: list[PlanLineageItem]


class PlanPreviewCounts(_StrictModel):
    move: int = Field(ge=0)
    unmapped: int = Field(ge=0)
    unchanged: int = Field(ge=0)


class PlanReviewCoverage(_StrictModel):
    total_unmapped: int = Field(ge=0)
    agent_explained: int = Field(ge=0)
    system_verified: int = Field(ge=0)
    fallback: int = Field(ge=0)


class PlanReviewSummary(_StrictModel):
    status: Literal[
        "agent_and_system",
        "system_only",
        "unavailable",
    ]
    agent_summary: str | None = Field(default=None, max_length=4096)
    advisory_only: Literal[True]
    coverage: PlanReviewCoverage


class PlanPreviewExplanation(_StrictModel):
    reason_code: Literal[
        "existing_episode",
        "possible_existing_movie",
        "extra_video",
        "ambiguous_mapping",
        "unsupported_content",
        "duplicate_candidate",
        "not_selected",
        "other",
    ]
    agent_detail: str | None = Field(default=None, max_length=1024)
    verification: Literal["verified", "advisory", "fallback"]
    season: int | None = Field(default=None, ge=0, le=999)
    episode: int | None = Field(default=None, ge=1, le=100_000)
    related_video_id: str | None


class _PlanPreviewItem(_StrictModel):
    index: int = Field(ge=0)
    candidate_id: str
    kind: Literal["video", "subtitle"]
    source: str


class MovePreviewItem(_PlanPreviewItem):
    disposition: Literal["move"]
    destination: str
    explanation: None


class UnmappedPreviewItem(_PlanPreviewItem):
    disposition: Literal["unmapped"]
    destination: None
    explanation: PlanPreviewExplanation


class UnchangedPreviewItem(_PlanPreviewItem):
    disposition: Literal["unchanged"]
    destination: None
    explanation: None


class PlanPreviewResponse(_StrictModel):
    run_id: str
    version: int = Field(ge=1)
    plan_hash: str
    plan_kind: Literal["initial", "amendment"]
    counts: PlanPreviewCounts
    review: PlanReviewSummary
    items: list[
        MovePreviewItem | UnmappedPreviewItem | UnchangedPreviewItem
    ]
    next_after: int | None = Field(default=None, ge=0)


class InteractionHistoryItem(_StrictModel):
    interaction_id: str
    kind: Literal["question", "revision", "reapply"]
    status: Literal["active", "completed", "failed"]
    request_message: str | None
    assistant_reply: str | None
    content_available: bool
    plan_hash: str | None
    created_at: str
    finished_at: str | None


class InteractionHistoryResponse(_StrictModel):
    items: list[InteractionHistoryItem]


class SafeEvent(_StrictModel):
    event_id: int = Field(ge=0)
    event_type: str
    data: dict[str, str | int | bool | None]


class EventsResponse(_StrictModel):
    items: list[SafeEvent]


class RunDeletionResponse(_StrictModel):
    run_id: str
    deleted_at: str


class ConfigWatchResponse(_StrictModel):
    watch_id: str
    work_type: Literal["anime", "tv", "movie"]
    poll_interval_seconds: int
    settle_interval_seconds: int
    root: str
    library_root: str


class ConfigProviderResponse(_StrictModel):
    base_url: str
    model: str
    reasoning_effort: str | None
    verbosity: str | None
    api_key_configured: bool


NotificationName = Literal[
    "plan_ready",
    "archive_completed",
    "attention_required",
]


class ConfigTelegramResponse(_StrictModel):
    enabled: bool
    notification_types: list[NotificationName]
    destination_configured: bool


class ConfigAcgripResponse(_StrictModel):
    enabled: bool


class ConfigAgentBudget(_StrictModel):
    max_model_turns: int = Field(ge=1, le=MAX_MODEL_TURNS)
    max_tool_calls: int = Field(ge=1, le=MAX_TOOL_CALLS)
    max_failures: int = Field(ge=1, le=MAX_FAILURES)
    max_total_tokens: int = Field(
        ge=1,
        le=MAX_TOTAL_TOKENS,
    )
    max_elapsed_seconds: float = Field(ge=1, le=3_600)


class ConfigResponse(_StrictModel):
    revision: int = Field(ge=1)
    revision_id: str
    watches: list[ConfigWatchResponse]
    provider: ConfigProviderResponse
    telegram: ConfigTelegramResponse
    acgrip: ConfigAcgripResponse
    apply_policy: Literal["plan_only", "manual", "automatic"]
    subtitle_acquisition_policy: Literal[
        "plan_only", "manual", "automatic"
    ]
    agent_budget: ConfigAgentBudget


class RootRetain(_StrictModel):
    mode: Literal["retain"]


class RootReplace(_StrictModel):
    mode: Literal["replace"]
    path: str


RootInput = str | RootRetain | RootReplace


class ConfigWatchRequest(_StrictModel):
    watch_id: str
    work_type: Literal["anime", "tv", "movie"]
    poll_interval_seconds: int
    settle_interval_seconds: int
    root: RootInput
    library_root: RootInput


class _ProviderRequest(_StrictModel):
    base_url: str
    model: str
    reasoning_effort: str | None
    verbosity: str | None


class LegacyProviderRequest(_ProviderRequest):
    api_key: str


class CredentialRetain(_StrictModel):
    mode: Literal["retain"]


class CredentialReplace(_StrictModel):
    mode: Literal["replace"]
    api_key: str


class TelegramDestinationRetain(_StrictModel):
    mode: Literal["retain"]


class TelegramDestinationUnset(_StrictModel):
    mode: Literal["unset"]


class TelegramDestinationReplace(_StrictModel):
    mode: Literal["replace"]
    bot_token: str
    chat_id: str


class TelegramConfigRequest(_StrictModel):
    enabled: bool
    notification_types: list[NotificationName] = Field(
        min_length=1,
        max_length=3,
    )
    destination: (
        TelegramDestinationRetain
        | TelegramDestinationUnset
        | TelegramDestinationReplace
    )


class AcgripConfigRequest(_StrictModel):
    enabled: bool


class EditProviderRequest(_ProviderRequest):
    credential: CredentialRetain | CredentialReplace


class ConfigUpdateRequest(_StrictModel):
    watches: list[ConfigWatchRequest]
    provider: LegacyProviderRequest | EditProviderRequest
    apply_policy: Literal["plan_only", "manual", "automatic"]
    agent_budget: ConfigAgentBudget | None = None
    telegram: TelegramConfigRequest | None = None
    acgrip: AcgripConfigRequest | None = None
    subtitle_acquisition_policy: Literal[
        "plan_only", "manual", "automatic"
    ] | None = None


class ProviderProbeRequest(_StrictModel):
    pass


class ProviderProbeResponse(_StrictModel):
    available: bool
    status_code: int | None


class TelegramTestRequest(_StrictModel):
    pass


class TelegramTestResponse(_StrictModel):
    notification_id: str
    state: Literal["queued"]


class MoveCapabilityCheck(_StrictModel):
    status: Literal[
        "supported",
        "degraded",
        "unsupported",
        "cross_filesystem",
        "uncertain",
    ]
    failure_code: str | None


class MoveCapabilityProbeRequest(_StrictModel):
    pass


class MoveCapabilityResponse(_StrictModel):
    watch_id: str
    move_backend: Literal["native", "fuse_checked_rename"]
    folder_disposition: MoveCapabilityCheck
    media_apply: MoveCapabilityCheck


class InteractionRequest(_StrictModel):
    kind: Literal["question", "revision"]
    message: str


class InteractionResponse(_StrictModel):
    interaction_id: str
    kind: Literal["question", "revision"]
    assistant_reply: str
    plan_hash: str | None
    model_tokens: int = Field(ge=0)


class AttentionControlRequest(_StrictModel):
    pass


class AttentionRetryResponse(_StrictModel):
    run_id: str
    status: Literal["retry_scheduled"]
    retry_count: int = Field(ge=1, le=3)


class AttentionFailResponse(_StrictModel):
    run_id: str
    status: Literal["failure_planned"]
    plan_hash: str


class SubtitleAcquisitionFailResponse(_StrictModel):
    run_id: str
    plan_hash: str
    status: Literal["failed"]
    failure_code: str


class ReapplyRequest(_StrictModel):
    message: str


class ReapplyResponse(_StrictModel):
    interaction_id: str
    assistant_reply: str
    plan_hash: str | None
    no_op: bool


class ApproveApplyRequest(_StrictModel):
    automatic: bool
    folder_disposition_plan_hash: str | None = None


class ForwardExecuteRequest(_StrictModel):
    pass


class ForwardExecuteResponse(ForwardExecutionView):
    run_id: str


class SubtitleAcquisitionApprovalRequest(_StrictModel):
    pass


class SubtitleAcquisitionResponse(SubtitleAcquisitionView):
    run_id: str


class FolderDispositionResultResponse(_StrictModel):
    run_id: str
    plan_hash: str
    approval_id: str
    transaction_id: str
    action: Literal["archive", "fail", "remove_empty"]
    target_relative: str | None
    status: Literal["completed"]


class ApplyResponse(_StrictModel):
    transaction_id: str
    plan_hash: str
    approval_id: str
    status: Literal["completed", "rolled_back"]
    applied_count: int = Field(ge=0)
    rolled_back_count: int = Field(ge=0)
    folder_disposition: FolderDispositionResultResponse | None = None


class RecoveryRequest(_StrictModel):
    approval_id: str


class RecoveryResponse(_StrictModel):
    transaction_id: str
    status: Literal["completed", "rolled_back"]
    applied_count: int = Field(ge=0)
    rolled_back_count: int = Field(ge=0)


class FolderDispositionRequest(_StrictModel):
    plan_hash: str
    automatic: bool


class FolderDispositionRecoveryRequest(_StrictModel):
    plan_hash: str
    approval_id: str
