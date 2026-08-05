from __future__ import annotations

from dataclasses import replace

import pytest

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.interactions import (
    InMemoryInteractionRepository,
    InteractionExecution,
    InteractionKind,
    InteractionService,
)


def test_idempotent_question_calls_model_once_and_is_domain_read_only() -> None:
    repository = InMemoryInteractionRepository(
        run_id="run-1",
        plan_hash="sha256:" + "a" * 64,
        session_revision=3,
    )
    calls = 0

    def execute(_: object) -> InteractionExecution:
        nonlocal calls
        calls += 1
        return InteractionExecution(
            assistant_reply="The mapping uses episode 1.",
            session_revision=4,
            model_tokens=12,
        )

    service = InteractionService(repository=repository, execute=execute)
    first = service.run(
        run_id="run-1",
        kind=InteractionKind.QUESTION,
        idempotency_key="idem-1",
        expected_plan_hash="sha256:" + "a" * 64,
        message="Why episode 1?",
    )
    second = service.run(
        run_id="run-1",
        kind=InteractionKind.QUESTION,
        idempotency_key="idem-1",
        expected_plan_hash="sha256:" + "a" * 64,
        message="Why episode 1?",
    )

    assert first == second
    assert calls == 1
    assert repository.plan_hash == "sha256:" + "a" * 64
    assert repository.domain_event_count == 0


def test_planless_attention_question_is_bound_to_event_sequence() -> None:
    repository = InMemoryInteractionRepository(
        run_id="run-1",
        plan_hash=None,
        session_revision=3,
        event_sequence=37,
    )
    service = InteractionService(
        repository=repository,
        execute=lambda _: InteractionExecution(
            assistant_reply="The subtitle evidence was ambiguous.",
            session_revision=4,
            model_tokens=12,
        ),
    )

    result = service.run(
        run_id="run-1",
        kind=InteractionKind.QUESTION,
        idempotency_key="idem-attention",
        expected_plan_hash=None,
        expected_event_sequence=37,
        message="Why does this need attention?",
    )

    assert result.plan_hash is None
    assert repository.plan_hash is None

    with pytest.raises(ServerError) as raised:
        service.run(
            run_id="run-1",
            kind=InteractionKind.QUESTION,
            idempotency_key="idem-stale",
            expected_plan_hash=None,
            expected_event_sequence=36,
            message="Try a stale page.",
        )
    assert raised.value.code is ServerErrorCode.INTERACTION_CONFLICT


def test_question_cannot_finalize_with_domain_mutation() -> None:
    repository = InMemoryInteractionRepository(
        run_id="run-1",
        plan_hash="sha256:" + "a" * 64,
        session_revision=1,
    )
    service = InteractionService(
        repository=repository,
        execute=lambda _: InteractionExecution(
            assistant_reply="mutated",
            session_revision=2,
            model_tokens=1,
            domain_events=("mapping_submitted",),
        ),
    )

    with pytest.raises(ServerError) as raised:
        service.run(
            run_id="run-1",
            kind=InteractionKind.QUESTION,
            idempotency_key="idem-question",
            expected_plan_hash="sha256:" + "a" * 64,
            message="change it",
        )

    assert raised.value.code is ServerErrorCode.INTERACTION_INVALID_RESULT


def test_revision_requires_fresh_complete_mapping_and_new_plan() -> None:
    repository = InMemoryInteractionRepository(
        run_id="run-1",
        plan_hash="sha256:" + "a" * 64,
        session_revision=2,
    )
    invalid = InteractionService(
        repository=repository,
        execute=lambda _: InteractionExecution(
            assistant_reply="looks good",
            session_revision=3,
            model_tokens=3,
            plan_hash="sha256:" + "b" * 64,
        ),
    )

    with pytest.raises(ServerError) as raised:
        invalid.run(
            run_id="run-1",
            kind=InteractionKind.REVISION,
            idempotency_key="idem-revision-bad",
            expected_plan_hash="sha256:" + "a" * 64,
            message="Episode 2 should be special 1.",
        )
    assert raised.value.code is ServerErrorCode.FRESH_MAPPING_REQUIRED

    repository.reconcile_active()
    valid = InteractionService(
        repository=repository,
        execute=lambda _: InteractionExecution(
            assistant_reply="Revised.",
            session_revision=3,
            model_tokens=7,
            domain_events=("mapping_submitted", "plan_built"),
            plan_hash="sha256:" + "c" * 64,
            fresh_mapping_submitted=True,
            lineage_parent_hash="sha256:" + "a" * 64,
        ),
    )
    result = valid.run(
        run_id="run-1",
        kind=InteractionKind.REVISION,
        idempotency_key="idem-revision-good",
        expected_plan_hash="sha256:" + "a" * 64,
        message="Episode 2 should be special 1.",
    )

    assert result.plan_hash == "sha256:" + "c" * 64
    assert repository.plan_hash == result.plan_hash
    assert repository.plan_versions == 2


def test_only_one_active_operation_per_run() -> None:
    repository = InMemoryInteractionRepository(
        run_id="run-1",
        plan_hash="sha256:" + "a" * 64,
        session_revision=1,
    )
    first = repository.reserve(
        run_id="run-1",
        kind=InteractionKind.QUESTION,
        idempotency_key="one",
        expected_plan_hash="sha256:" + "a" * 64,
        message="one",
    )

    with pytest.raises(ServerError) as raised:
        repository.reserve(
            run_id="run-1",
            kind=InteractionKind.REVISION,
            idempotency_key="two",
            expected_plan_hash="sha256:" + "a" * 64,
            message="two",
        )

    assert first.terminal_result is None
    assert raised.value.code is ServerErrorCode.RUN_BUSY
