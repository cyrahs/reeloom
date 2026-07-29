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
            "reapply",
            "recover",
            "settle_folder",
            "dispose_failed_folder",
            "recover_folder_disposition",
            "delete_run",
        ]
    ]
    settlement: RunSettlement | None
    source_folder: str | None = None
    folder_disposition: FolderDispositionView | None = None
    archive_report: ArchiveReport | None


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


class _PlanPreviewItem(_StrictModel):
    index: int = Field(ge=0)
    candidate_id: str
    kind: Literal["video", "subtitle"]
    source: str


class MovePreviewItem(_PlanPreviewItem):
    disposition: Literal["move"]
    destination: str


class UnmappedPreviewItem(_PlanPreviewItem):
    disposition: Literal["unmapped"]
    destination: None


class UnchangedPreviewItem(_PlanPreviewItem):
    disposition: Literal["unchanged"]
    destination: None


class PlanPreviewResponse(_StrictModel):
    run_id: str
    version: int = Field(ge=1)
    plan_hash: str
    plan_kind: Literal["initial", "amendment"]
    counts: PlanPreviewCounts
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
    apply_policy: Literal["plan_only", "manual", "automatic"]
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


class EditProviderRequest(_ProviderRequest):
    credential: CredentialRetain | CredentialReplace


class ConfigUpdateRequest(_StrictModel):
    watches: list[ConfigWatchRequest]
    provider: LegacyProviderRequest | EditProviderRequest
    apply_policy: Literal["plan_only", "manual", "automatic"]
    agent_budget: ConfigAgentBudget | None = None


class ProviderProbeRequest(_StrictModel):
    pass


class ProviderProbeResponse(_StrictModel):
    available: bool
    status_code: int | None


class MoveCapabilityCheck(_StrictModel):
    status: Literal[
        "supported",
        "unsupported",
        "cross_filesystem",
        "uncertain",
    ]
    failure_code: str | None


class MoveCapabilityProbeRequest(_StrictModel):
    pass


class MoveCapabilityResponse(_StrictModel):
    watch_id: str
    move_backend: Literal["native"]
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
