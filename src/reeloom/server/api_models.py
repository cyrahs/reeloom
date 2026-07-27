from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ErrorBody(_StrictModel):
    code: str


class ErrorResponse(_StrictModel):
    error: ErrorBody


class SessionResponse(_StrictModel):
    api_version: Literal["1.0.0"]
    role: Literal["viewer", "operator", "admin"]


class HealthResponse(_StrictModel):
    status: Literal["ok"]
    postgres_major: int = Field(ge=1)
    schema_version: int = Field(ge=1)


class RunSummary(_StrictModel):
    run_id: str
    status: str
    work_type: Literal["anime", "tv", "movie"]
    created_at: str
    phase: str | None
    plan_hash: str | None


class RunsResponse(_StrictModel):
    items: list[RunSummary]


class DiscoverySummary(_StrictModel):
    discovery_id: str
    watch_id: str
    work_type: Literal["anime", "tv", "movie"]
    discovered_at: str
    run_id: str | None
    run_status: str | None


class DiscoveriesResponse(_StrictModel):
    items: list[DiscoverySummary]


class RunSettlement(_StrictModel):
    approval_id: str
    plan_hash: str
    transaction_id: str
    status: Literal["completed", "rolled_back"]
    applied_count: int = Field(ge=0)
    rolled_back_count: int = Field(ge=0)
    failure_code: str | None
    settled_at: str


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
        ]
    ]
    settlement: RunSettlement | None


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


class ConfigWatchResponse(_StrictModel):
    watch_id: str
    work_type: Literal["anime", "tv", "movie"]
    poll_interval_seconds: int
    settle_interval_seconds: int
    root_configured: bool


class ConfigRouteResponse(_StrictModel):
    work_type: Literal["anime", "tv", "movie"]
    root_configured: bool


class ConfigProviderResponse(_StrictModel):
    base_url: str
    model: str
    reasoning_effort: str | None
    verbosity: str | None
    api_key_configured: bool


class ConfigResponse(_StrictModel):
    revision: int = Field(ge=1)
    revision_id: str
    watches: list[ConfigWatchResponse]
    archive_routes: list[ConfigRouteResponse]
    provider: ConfigProviderResponse
    apply_policy: Literal["plan_only", "manual", "automatic"]


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


class ConfigRouteRequest(_StrictModel):
    work_type: Literal["anime", "tv", "movie"]
    root: RootInput


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
    archive_routes: list[ConfigRouteRequest]
    provider: LegacyProviderRequest | EditProviderRequest
    apply_policy: Literal["plan_only", "manual", "automatic"]


class ProviderProbeRequest(_StrictModel):
    pass


class ProviderProbeResponse(_StrictModel):
    available: bool
    status_code: int | None


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


class ApplyResponse(_StrictModel):
    transaction_id: str
    plan_hash: str
    approval_id: str
    status: Literal["completed", "rolled_back"]
    applied_count: int = Field(ge=0)
    rolled_back_count: int = Field(ge=0)


class RecoveryRequest(_StrictModel):
    approval_id: str


class RecoveryResponse(_StrictModel):
    transaction_id: str
    status: Literal["completed", "rolled_back"]
    applied_count: int = Field(ge=0)
    rolled_back_count: int = Field(ge=0)
