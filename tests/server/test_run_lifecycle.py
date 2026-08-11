from __future__ import annotations

from dataclasses import replace

import pytest

from reeloom.kernel.forward_execution import ExecutionOperationStatus
from reeloom.server.config import ApplyPolicy
from reeloom.server.run_lifecycle import (
    RunActionKind,
    RunEffectKind,
    RunEffectMode,
    RunLifecycleFacts,
    RunLifecycleState,
    derive_run_lifecycle,
    resolve_run_action,
)

_HASH = "sha256:" + "a" * 64


def _facts(**changes: object) -> RunLifecycleFacts:
    base = RunLifecycleFacts(
        run_id="run:m14-6",
        mode=RunEffectMode.FORWARD_V2,
        revision=1,
        stored_status="running",
        runtime_status="stopped",
        runtime_phase="awaiting_approval",
        event_sequence=7,
    )
    return replace(base, **changes)


def test_terminal_operation_is_authoritative_over_agent_history() -> None:
    facts = _facts(
        effect_kind=RunEffectKind.SUBTITLE_ACQUIRE,
        effect_plan_hash=_HASH,
        effect_policy=ApplyPolicy.AUTOMATIC,
        operation_id="operation:m14-6",
        operation_status=ExecutionOperationStatus.COMPLETED,
    )

    presentation = derive_run_lifecycle(facts)

    assert presentation.state is RunLifecycleState.COMPLETED
    assert presentation.terminal
    assert [item.kind for item in presentation.actions] == [
        RunActionKind.DELETE_RUN
    ]


@pytest.mark.parametrize(
    ("policy", "state", "actions"),
    (
        (ApplyPolicy.PLAN_ONLY, RunLifecycleState.COMPLETED, ("delete_run",)),
        (
            ApplyPolicy.MANUAL,
            RunLifecycleState.AWAITING_APPROVAL,
            ("execute",),
        ),
        (ApplyPolicy.AUTOMATIC, RunLifecycleState.EXECUTION_QUEUED, ()),
    ),
)
def test_plan_handoff_policy_has_one_canonical_outcome(
    policy: ApplyPolicy,
    state: RunLifecycleState,
    actions: tuple[str, ...],
) -> None:
    presentation = derive_run_lifecycle(
        _facts(
            effect_kind=RunEffectKind.MEDIA_MOVE,
            effect_plan_hash=_HASH,
            effect_policy=policy,
            planning_terminal_outcome=(
                "plan_only" if policy is ApplyPolicy.PLAN_ONLY else None
            ),
            interaction_budget_available=False,
        )
    )

    assert presentation.state is state
    assert tuple(item.kind.value for item in presentation.actions) == actions


def test_plan_only_policy_without_terminal_fact_is_not_reported_complete() -> None:
    presentation = derive_run_lifecycle(
        _facts(
            effect_kind=RunEffectKind.MEDIA_MOVE,
            effect_plan_hash=_HASH,
            effect_policy=ApplyPolicy.PLAN_ONLY,
        )
    )

    assert presentation.state is RunLifecycleState.PLANNING
    assert not presentation.terminal
    assert presentation.actions == ()


def test_manual_subtitle_plan_does_not_advertise_media_revision() -> None:
    presentation = derive_run_lifecycle(
        _facts(
            effect_kind=RunEffectKind.SUBTITLE_ACQUIRE,
            effect_plan_hash=_HASH,
            effect_policy=ApplyPolicy.MANUAL,
            interaction_budget_available=True,
        )
    )

    assert presentation.state is RunLifecycleState.AWAITING_APPROVAL
    assert [item.kind for item in presentation.actions] == [
        RunActionKind.EXECUTE
    ]


def test_failed_operation_exposes_rescan_without_browser_plan_hash() -> None:
    facts = _facts(
        effect_kind=RunEffectKind.SUBTITLE_ACQUIRE,
        effect_plan_hash=_HASH,
        effect_policy=ApplyPolicy.AUTOMATIC,
        operation_id="operation:m14-6",
        operation_status=ExecutionOperationStatus.COLLISION,
    )

    presentation = derive_run_lifecycle(facts)

    assert [item.kind for item in presentation.actions] == [
        RunActionKind.REQUEST_RESCAN,
        RunActionKind.DELETE_RUN,
    ]
    assert resolve_run_action(
        facts, presentation.actions[0].action_id
    ) == presentation.actions[0]
    assert resolve_run_action(
        replace(facts, revision=2), presentation.actions[0].action_id
    ) is None


def test_pending_rescan_or_successor_suppresses_duplicate_action() -> None:
    base = _facts(
        effect_kind=RunEffectKind.MEDIA_MOVE,
        effect_plan_hash=_HASH,
        effect_policy=ApplyPolicy.MANUAL,
        operation_id="operation:m14-6",
        operation_status=ExecutionOperationStatus.STALE,
    )

    for facts in (
        replace(base, rescan_state="queued"),
        replace(base, rescan_state="accepted"),
        replace(base, successor_run_id="run:successor"),
    ):
        assert [
            item.kind for item in derive_run_lifecycle(facts).actions
        ] == [RunActionKind.DELETE_RUN]


def test_completed_subtitle_with_blocked_successor_exposes_rescan() -> None:
    facts = _facts(
        effect_kind=RunEffectKind.SUBTITLE_ACQUIRE,
        effect_plan_hash=_HASH,
        effect_policy=ApplyPolicy.AUTOMATIC,
        operation_id="operation:m14-6",
        operation_status=ExecutionOperationStatus.COMPLETED,
        rescan_state="blocked",
    )

    presentation = derive_run_lifecycle(facts)

    assert presentation.state is RunLifecycleState.COMPLETED
    assert [item.kind for item in presentation.actions] == [
        RunActionKind.REQUEST_RESCAN,
        RunActionKind.DELETE_RUN,
    ]


def test_superseded_operation_never_advertises_rescan() -> None:
    presentation = derive_run_lifecycle(
        _facts(
            effect_kind=RunEffectKind.MEDIA_MOVE,
            effect_plan_hash=_HASH,
            effect_policy=ApplyPolicy.AUTOMATIC,
            operation_id="operation:old",
            operation_status=ExecutionOperationStatus.SUPERSEDED,
        )
    )

    assert presentation.state is RunLifecycleState.FAILED
    assert [item.kind for item in presentation.actions] == [
        RunActionKind.DELETE_RUN
    ]


def test_legacy_history_never_exposes_effect_action() -> None:
    presentation = derive_run_lifecycle(
        _facts(
            mode=RunEffectMode.LEGACY_READ_ONLY,
            stored_status="superseded",
            effect_kind=RunEffectKind.MEDIA_MOVE,
            effect_plan_hash=_HASH,
            effect_policy=ApplyPolicy.MANUAL,
        )
    )

    assert presentation.state is RunLifecycleState.LEGACY_READ_ONLY
    assert [item.kind for item in presentation.actions] == [
        RunActionKind.DELETE_RUN
    ]


def test_nonterminal_legacy_history_still_has_delete_escape() -> None:
    presentation = derive_run_lifecycle(
        _facts(mode=RunEffectMode.LEGACY_READ_ONLY, stored_status="running")
    )

    assert presentation.terminal
    assert [item.kind for item in presentation.actions] == [
        RunActionKind.DELETE_RUN
    ]


def test_needs_attention_actions_are_bounded_and_stable() -> None:
    facts = _facts(
        needs_attention=True,
        interaction_budget_available=True,
        can_retry_agent=True,
        can_mark_failed=True,
        retry_count=1,
    )

    first = derive_run_lifecycle(facts)
    second = derive_run_lifecycle(facts)

    assert first == second
    assert [item.kind for item in first.actions] == [
        RunActionKind.ASK_AGENT,
        RunActionKind.RETRY_AGENT,
        RunActionKind.MARK_FAILED,
    ]
