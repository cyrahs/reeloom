from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from reeloom.kernel.forward_execution import ExecutionOperationStatus
from reeloom.server.config import ApplyPolicy

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PLAN_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class RunEffectMode(StrEnum):
    FORWARD_V2 = "forward_v2"
    LEGACY_READ_ONLY = "legacy_read_only"


class RunEffectKind(StrEnum):
    MEDIA_MOVE = "media_move"
    SUBTITLE_ACQUIRE = "subtitle_acquire"


class RunLifecycleState(StrEnum):
    PLANNING = "planning"
    NEEDS_ATTENTION = "needs_attention"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTION_QUEUED = "execution_queued"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    LEGACY_READ_ONLY = "legacy_read_only"
    DELETED = "deleted"


class RunActionKind(StrEnum):
    ASK_AGENT = "ask_agent"
    REVISE_PLAN = "revise_plan"
    EXECUTE = "execute"
    REQUEST_RESCAN = "request_rescan"
    RETRY_AGENT = "retry_agent"
    MARK_FAILED = "mark_failed"
    DELETE_RUN = "delete_run"


class RunActionInput(StrEnum):
    NONE = "none"
    MESSAGE = "message"
    CONFIRMATION = "confirmation"


@dataclass(frozen=True, slots=True)
class RunLifecycleFacts:
    """Current durable facts used by every v2 control-plane consumer.

    ``runs.status`` and ``run_states.phase`` are compatibility/history fields;
    they are intentionally lower priority than the current effect operation.
    """

    run_id: str
    mode: RunEffectMode
    revision: int
    stored_status: str
    runtime_status: str | None = None
    runtime_phase: str | None = None
    event_sequence: int = 0
    effect_kind: RunEffectKind | None = None
    effect_plan_hash: str | None = None
    effect_policy: ApplyPolicy | None = None
    operation_id: str | None = None
    operation_status: ExecutionOperationStatus | None = None
    planning_terminal_outcome: str | None = None
    rescan_state: str | None = None
    successor_run_id: str | None = None
    needs_attention: bool = False
    interaction_budget_available: bool = False
    retry_count: int = 0
    retry_limit: int = 3
    can_retry_agent: bool = False
    can_mark_failed: bool = False
    active_interaction: bool = False
    deleted: bool = False

    def __post_init__(self) -> None:
        if (
            _IDENTIFIER.fullmatch(self.run_id) is None
            or type(self.revision) is not int
            or self.revision < 0
            or type(self.event_sequence) is not int
            or self.event_sequence < 0
            or type(self.retry_count) is not int
            or type(self.retry_limit) is not int
            or not 0 <= self.retry_count <= self.retry_limit <= 100
        ):
            raise ValueError("invalid lifecycle facts")
        head = (
            self.effect_kind,
            self.effect_plan_hash,
            self.effect_policy,
        )
        if any(item is None for item in head) != all(
            item is None for item in head
        ):
            raise ValueError("incomplete effect head")
        if (
            self.effect_plan_hash is not None
            and _PLAN_HASH.fullmatch(self.effect_plan_hash) is None
        ):
            raise ValueError("invalid plan hash")
        if self.operation_id is not None and (
            _IDENTIFIER.fullmatch(self.operation_id) is None
            or self.effect_plan_hash is None
            or self.operation_status is None
        ):
            raise ValueError("invalid operation binding")
        if self.operation_status is not None and self.operation_id is None:
            raise ValueError("operation status without operation")
        if self.planning_terminal_outcome not in {
            None,
            "plan_only",
            "user_failed",
            "agent_failed",
            "unsupported_source",
            "migration_quarantine",
        }:
            raise ValueError("invalid planning terminal outcome")
        if self.successor_run_id is not None and (
            _IDENTIFIER.fullmatch(self.successor_run_id) is None
        ):
            raise ValueError("invalid successor")


@dataclass(frozen=True, slots=True)
class BoundRunAction:
    action_id: str
    kind: RunActionKind
    input: RunActionInput
    destructive: bool


@dataclass(frozen=True, slots=True)
class RunLifecyclePresentation:
    schema_version: int
    mode: RunEffectMode
    state: RunLifecycleState
    terminal: bool
    revision: int
    effect_kind: RunEffectKind | None
    effect_plan_hash: str | None
    effect_policy: ApplyPolicy | None
    operation_id: str | None
    operation_status: ExecutionOperationStatus | None
    rescan_state: str | None
    successor_run_id: str | None
    actions: tuple[BoundRunAction, ...]
    etag: str


def _state(facts: RunLifecycleFacts) -> RunLifecycleState:
    if facts.deleted:
        return RunLifecycleState.DELETED
    if facts.mode is RunEffectMode.LEGACY_READ_ONLY:
        return RunLifecycleState.LEGACY_READ_ONLY
    if facts.operation_status is ExecutionOperationStatus.AUTHORIZED:
        return RunLifecycleState.EXECUTION_QUEUED
    if facts.operation_status is ExecutionOperationStatus.RUNNING:
        return RunLifecycleState.EXECUTING
    if facts.operation_status is ExecutionOperationStatus.COMPLETED:
        return RunLifecycleState.COMPLETED
    if facts.operation_status is not None:
        return RunLifecycleState.FAILED
    if facts.planning_terminal_outcome == "plan_only":
        return RunLifecycleState.COMPLETED
    if facts.planning_terminal_outcome is not None:
        return RunLifecycleState.FAILED
    if facts.effect_policy is ApplyPolicy.MANUAL:
        return RunLifecycleState.AWAITING_APPROVAL
    if facts.effect_policy is ApplyPolicy.AUTOMATIC:
        return RunLifecycleState.EXECUTION_QUEUED
    if facts.needs_attention:
        return RunLifecycleState.NEEDS_ATTENTION
    if facts.stored_status in {"completed", "superseded"}:
        return RunLifecycleState.COMPLETED
    if facts.stored_status in {"failed", "rolled_back"}:
        return RunLifecycleState.FAILED
    return RunLifecycleState.PLANNING


def _action_id(facts: RunLifecycleFacts, kind: RunActionKind) -> str:
    canonical = json.dumps(
        {
            "event_sequence": facts.event_sequence,
            "kind": kind.value,
            "operation_id": facts.operation_id,
            "operation_status": (
                None
                if facts.operation_status is None
                else facts.operation_status.value
            ),
            "plan_hash": facts.effect_plan_hash,
            "rescan_state": facts.rescan_state,
            "revision": facts.revision,
            "run_id": facts.run_id,
            "successor_run_id": facts.successor_run_id,
            "version": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "runaction-v1:" + hashlib.sha256(canonical).hexdigest()


def _actions(
    facts: RunLifecycleFacts,
    state: RunLifecycleState,
) -> tuple[BoundRunAction, ...]:
    if facts.active_interaction or state is RunLifecycleState.DELETED:
        return ()
    definitions: list[tuple[RunActionKind, RunActionInput, bool]] = []
    if state is RunLifecycleState.NEEDS_ATTENTION:
        if facts.interaction_budget_available:
            definitions.append(
                (RunActionKind.ASK_AGENT, RunActionInput.MESSAGE, False)
            )
        if facts.can_retry_agent and facts.retry_count < facts.retry_limit:
            definitions.append(
                (RunActionKind.RETRY_AGENT, RunActionInput.NONE, False)
            )
        if facts.can_mark_failed:
            definitions.append(
                (
                    RunActionKind.MARK_FAILED,
                    RunActionInput.CONFIRMATION,
                    True,
                )
            )
    elif state is RunLifecycleState.AWAITING_APPROVAL:
        if (
            facts.effect_kind is RunEffectKind.MEDIA_MOVE
            and facts.interaction_budget_available
        ):
            definitions.append(
                (RunActionKind.REVISE_PLAN, RunActionInput.MESSAGE, False)
            )
        definitions.append(
            (RunActionKind.EXECUTE, RunActionInput.CONFIRMATION, True)
        )
    elif state is RunLifecycleState.FAILED:
        if (
            facts.mode is RunEffectMode.FORWARD_V2
            and facts.operation_id is not None
            and facts.operation_status
            is not ExecutionOperationStatus.SUPERSEDED
            and facts.successor_run_id is None
            and facts.rescan_state
            not in {"queued", "leased", "accepted", "retry_wait", "completed"}
        ):
            definitions.append(
                (
                    RunActionKind.REQUEST_RESCAN,
                    RunActionInput.CONFIRMATION,
                    False,
                )
            )
        definitions.append(
            (RunActionKind.DELETE_RUN, RunActionInput.CONFIRMATION, True)
        )
    elif state is RunLifecycleState.COMPLETED:
        if (
            facts.mode is RunEffectMode.FORWARD_V2
            and facts.operation_id is not None
            and facts.rescan_state == "blocked"
            and facts.successor_run_id is None
        ):
            definitions.append(
                (
                    RunActionKind.REQUEST_RESCAN,
                    RunActionInput.CONFIRMATION,
                    False,
                )
            )
        definitions.append(
            (RunActionKind.DELETE_RUN, RunActionInput.CONFIRMATION, True)
        )
    elif state is RunLifecycleState.LEGACY_READ_ONLY:
        definitions.append(
            (RunActionKind.DELETE_RUN, RunActionInput.CONFIRMATION, True)
        )
    return tuple(
        BoundRunAction(
            action_id=_action_id(facts, kind),
            kind=kind,
            input=input_kind,
            destructive=destructive,
        )
        for kind, input_kind, destructive in definitions
    )


def derive_run_lifecycle(
    facts: RunLifecycleFacts,
) -> RunLifecyclePresentation:
    state = _state(facts)
    actions = _actions(facts, state)
    payload = json.dumps(
        {
            "actions": [item.action_id for item in actions],
            "mode": facts.mode.value,
            "operation_id": facts.operation_id,
            "operation_status": (
                None
                if facts.operation_status is None
                else facts.operation_status.value
            ),
            "plan_hash": facts.effect_plan_hash,
            "rescan_state": facts.rescan_state,
            "revision": facts.revision,
            "state": state.value,
            "successor_run_id": facts.successor_run_id,
            "version": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return RunLifecyclePresentation(
        schema_version=1,
        mode=facts.mode,
        state=state,
        terminal=state
        in {
            RunLifecycleState.COMPLETED,
            RunLifecycleState.FAILED,
            RunLifecycleState.LEGACY_READ_ONLY,
            RunLifecycleState.DELETED,
        },
        revision=facts.revision,
        effect_kind=facts.effect_kind,
        effect_plan_hash=facts.effect_plan_hash,
        effect_policy=facts.effect_policy,
        operation_id=facts.operation_id,
        operation_status=facts.operation_status,
        rescan_state=facts.rescan_state,
        successor_run_id=facts.successor_run_id,
        actions=actions,
        etag="runpresentation-v1:"
        + hashlib.sha256(payload).hexdigest(),
    )


def resolve_run_action(
    facts: RunLifecycleFacts,
    action_id: str,
) -> BoundRunAction | None:
    return next(
        (
            action
            for action in derive_run_lifecycle(facts).actions
            if action.action_id == action_id
        ),
        None,
    )
