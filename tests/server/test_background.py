from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from reeloom.executor.apply import ApplyResult, ApplyStatus
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.kernel.candidates import CandidateKind
from reeloom.server.background import (
    BackgroundServices,
    _semantic_watch_v2_enabled,
)
from reeloom.server.config import (
    ApplyPolicy,
    ServerWorkType,
    SubtitleAcquisitionPolicy,
)
from reeloom.server.agent_worker import AgentWorkKind, AgentWorkResult
from reeloom.server.errors import ServerError, ServerErrorCode


class _UnavailableConfigs:
    def head(self) -> None:
        raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE)


@pytest.mark.parametrize(
    ("policy", "work_type", "acgrip", "expected"),
    (
        (ApplyPolicy.PLAN_ONLY, ServerWorkType.ANIME, False, True),
        (ApplyPolicy.PLAN_ONLY, ServerWorkType.TV, False, True),
        (ApplyPolicy.PLAN_ONLY, ServerWorkType.MOVIE, False, False),
        (ApplyPolicy.MANUAL, ServerWorkType.ANIME, False, False),
        (ApplyPolicy.AUTOMATIC, ServerWorkType.ANIME, False, False),
        (ApplyPolicy.PLAN_ONLY, ServerWorkType.ANIME, True, False),
    ),
)
def test_semantic_watch_isolated_to_side_effect_free_episode_plans(
    policy: ApplyPolicy,
    work_type: ServerWorkType,
    acgrip: bool,
    expected: bool,
) -> None:
    config = SimpleNamespace(
        apply_policy=policy,
        acgrip=SimpleNamespace(enabled=acgrip),
    )

    assert _semantic_watch_v2_enabled(  # type: ignore[arg-type]
        config, work_type
    ) is expected


def test_database_unavailable_stops_background_fail_closed() -> None:
    background = BackgroundServices(
        boot_id="boot-test",
        configs=_UnavailableConfigs(),  # type: ignore[arg-type]
        scheduler=object(),  # type: ignore[arg-type]
        worker=object(),  # type: ignore[arg-type]
        apply=object(),  # type: ignore[arg-type]
        idle_seconds=0.001,
    )

    background.start()
    deadline = time.monotonic() + 1
    while not background.fatal and time.monotonic() < deadline:
        time.sleep(0.001)
    background.close(timeout_seconds=1)

    assert background.fatal


class _SettlingScheduler:
    def __init__(self) -> None:
        self.settled: list[bool] = []
        self.retried: list[tuple[str, str]] = []

    def get_job_context(self, *, run_id: str) -> object:
        del run_id
        return SimpleNamespace(
            registration=SimpleNamespace(config_revision=1)
        )

    def settle_job(
        self,
        *,
        job_id: str,
        boot_id: str,
        succeeded: bool,
    ) -> None:
        del job_id, boot_id
        self.settled.append(succeeded)

    def retry_job(self, *, job_id: str, boot_id: str) -> None:
        self.retried.append((job_id, boot_id))


class _UnavailableWorker:
    async def run(self, *, run_id: str) -> str:
        del run_id
        raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE)


class _SuccessfulWorker:
    async def run(self, *, run_id: str) -> str:
        del run_id
        return "sha256:" + "a" * 64


class _AutomaticConfigs:
    def get(self, revision: int) -> object:
        del revision
        return SimpleNamespace(apply_policy=ApplyPolicy.AUTOMATIC)


class _UnavailableApply:
    def approve_and_apply(self, **kwargs: object) -> object:
        del kwargs
        raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE)


class _SettledSourceDriftApply:
    def approve_and_apply(self, **kwargs: object) -> ApplyResult:
        return ApplyResult(
            transaction_id="txn-v1-" + "b" * 64,
            plan_hash=str(kwargs["plan_hash"]),
            approval_id="approval-v1-" + "c" * 64,
            status=ApplyStatus.ROLLED_BACK,
            applied_count=0,
            rolled_back_count=0,
            failure_code=ExecutorErrorCode.SOURCE_DRIFT,
        )


@pytest.mark.parametrize(
    ("worker", "configs", "apply"),
    (
        (_UnavailableWorker(), object(), object()),
        (_SuccessfulWorker(), _AutomaticConfigs(), _UnavailableApply()),
    ),
)
def test_database_failure_is_rethrown_without_settling_job(
    worker: object,
    configs: object,
    apply: object,
) -> None:
    scheduler = _SettlingScheduler()
    background = BackgroundServices(
        boot_id="boot-test",
        configs=configs,  # type: ignore[arg-type]
        scheduler=scheduler,  # type: ignore[arg-type]
        worker=worker,  # type: ignore[arg-type]
        apply=apply,  # type: ignore[arg-type]
    )

    with pytest.raises(ServerError) as raised:
        background._execute_job("job-test", "run-test")

    assert raised.value.code is ServerErrorCode.DATABASE_UNAVAILABLE
    assert scheduler.settled == []


def test_only_deterministic_executor_collision_is_terminal() -> None:
    assert (
        BackgroundServices._failure_reason(
            ExecutorError(ExecutorErrorCode.DESTINATION_COLLISION)
        )
        == "executor_destination_collision"
    )
    assert (
        BackgroundServices._failure_reason(
            ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
        )
        is None
    )


class _FolderScheduler:
    def __init__(
        self, *, retry_results: list[int | None] | None = None
    ) -> None:
        self.settled: list[bool] = []
        self.restarted = 0
        self.retry_results = list(retry_results or [])
        self.retry_calls: list[tuple[str, int]] = []
        self.failed = 0

    def get_job_context(self, *, run_id: str) -> object:
        del run_id
        return SimpleNamespace(
            registration=SimpleNamespace(config_revision=1),
            discovery=SimpleNamespace(
                folder_generation_id="generation-test",
                snapshot=SimpleNamespace(
                    files=(SimpleNamespace(kind=CandidateKind.VIDEO),)
                ),
            ),
        )

    def settle_job(self, **kwargs: object) -> None:
        self.settled.append(bool(kwargs["succeeded"]))

    def restart_folder_generation(self, *, run_id: str) -> None:
        del run_id
        self.restarted += 1

    def retry_folder_generation(
        self,
        *,
        run_id: str,
        max_retries: int,
    ) -> int | None:
        self.retry_calls.append((run_id, max_retries))
        return self.retry_results.pop(0)

    def mark_run_failed(self, *, run_id: str) -> None:
        del run_id
        self.failed += 1


class _FailingWorker:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def run(self, *, run_id: str) -> str:
        del run_id
        raise self.error


class _ManualConfigs:
    def get(self, revision: int) -> object:
        del revision
        return SimpleNamespace(apply_policy=ApplyPolicy.PLAN_ONLY)


class _SubtitleWorker:
    async def run_result(self, *, run_id: str) -> AgentWorkResult:
        del run_id
        return AgentWorkResult(
            AgentWorkKind.SUBTITLE_ACQUISITION,
            "sha256:" + "s" * 64,
        )


class _SubtitleConfigs:
    def __init__(self, policy: SubtitleAcquisitionPolicy) -> None:
        self.policy = policy

    def get(self, revision: int) -> object:
        del revision
        return SimpleNamespace(
            apply_policy=ApplyPolicy.PLAN_ONLY,
            subtitle_acquisition_policy=self.policy,
        )


class _SubtitleCoordinator:
    def __init__(self) -> None:
        self.executed: list[tuple[str, str, bool]] = []

    def approve_and_execute(self, **kwargs: object) -> object:
        self.executed.append(
            (
                str(kwargs["run_id"]),
                str(kwargs["plan_hash"]),
                bool(kwargs["automatic"]),
            )
        )
        return SimpleNamespace(status="published")

    def resolve(self, **kwargs: object) -> object:
        del kwargs
        return SimpleNamespace(status="published")


class _TransientSubtitleCoordinator(_SubtitleCoordinator):
    def approve_and_execute(self, **kwargs: object) -> object:
        del kwargs
        raise RuntimeError("temporary transport failure")

    def resolve(self, **kwargs: object) -> object:
        del kwargs
        return SimpleNamespace(status="approved")


@pytest.mark.parametrize(
    ("policy", "expected_execution", "expected_settlement"),
    (
        (SubtitleAcquisitionPolicy.AUTOMATIC, 1, ()),
        (SubtitleAcquisitionPolicy.MANUAL, 0, (True,)),
        (SubtitleAcquisitionPolicy.PLAN_ONLY, 0, (True,)),
    ),
)
def test_subtitle_acquisition_uses_independent_policy_and_never_media_apply(
    policy: SubtitleAcquisitionPolicy,
    expected_execution: int,
    expected_settlement: tuple[bool, ...],
) -> None:
    scheduler = _SettlingScheduler()
    coordinator = _SubtitleCoordinator()
    background = BackgroundServices(
        boot_id="boot-test",
        configs=_SubtitleConfigs(policy),  # type: ignore[arg-type]
        scheduler=scheduler,  # type: ignore[arg-type]
        worker=_SubtitleWorker(),  # type: ignore[arg-type]
        apply=_UnavailableApply(),  # type: ignore[arg-type]
        subtitle_acquisitions=coordinator,  # type: ignore[arg-type]
    )

    background._execute_job("job-test", "run-test")

    assert len(coordinator.executed) == expected_execution
    assert tuple(scheduler.settled) == expected_settlement


def test_transient_subtitle_failure_retries_same_job() -> None:
    scheduler = _SettlingScheduler()
    background = BackgroundServices(
        boot_id="boot-test",
        configs=_SubtitleConfigs(SubtitleAcquisitionPolicy.AUTOMATIC),
        scheduler=scheduler,  # type: ignore[arg-type]
        worker=_SubtitleWorker(),  # type: ignore[arg-type]
        apply=object(),  # type: ignore[arg-type]
        subtitle_acquisitions=_TransientSubtitleCoordinator(),
    )

    background._execute_job("job-test", "run-test")

    assert scheduler.retried == [("job-test", "boot-test")]
    assert scheduler.settled == []


class _FolderDispositions:
    def __init__(self) -> None:
        self.failures: list[tuple[str, str]] = []

    def prepare_failure(
        self, *, run_id: str, reason_code: str
    ) -> object:
        self.failures.append((run_id, reason_code))
        return SimpleNamespace(plan_hash="sha256:" + "f" * 64)


@pytest.mark.parametrize(
    "code",
    (
        ExecutorErrorCode.MOVE_FAILED,
        ExecutorErrorCode.PERMISSION_DENIED,
    ),
)
def test_executor_failure_does_not_restart_folder_generation(
    code: ExecutorErrorCode,
) -> None:
    scheduler = _FolderScheduler()
    background = BackgroundServices(
        boot_id="boot-test",
        configs=object(),  # type: ignore[arg-type]
        scheduler=scheduler,  # type: ignore[arg-type]
        worker=_FailingWorker(ExecutorError(code)),  # type: ignore[arg-type]
        apply=object(),  # type: ignore[arg-type]
    )

    background._execute_job("job-test", "run-test")

    assert scheduler.settled == [True]
    assert scheduler.restarted == 0
    assert scheduler.failed == 0


def test_only_source_drift_restarts_folder_generation() -> None:
    scheduler = _FolderScheduler()
    background = BackgroundServices(
        boot_id="boot-test",
        configs=object(),  # type: ignore[arg-type]
        scheduler=scheduler,  # type: ignore[arg-type]
        worker=_FailingWorker(
            ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
        ),  # type: ignore[arg-type]
        apply=object(),  # type: ignore[arg-type]
    )

    background._execute_job("job-test", "run-test")

    assert scheduler.settled == []
    assert scheduler.restarted == 1
    assert scheduler.failed == 0


def test_settled_preflight_source_drift_restarts_without_recovery() -> None:
    scheduler = _FolderScheduler()
    background = BackgroundServices(
        boot_id="boot-test",
        configs=_AutomaticConfigs(),  # type: ignore[arg-type]
        scheduler=scheduler,  # type: ignore[arg-type]
        worker=_SuccessfulWorker(),  # type: ignore[arg-type]
        apply=_SettledSourceDriftApply(),  # type: ignore[arg-type]
    )

    background._execute_job("job-test", "run-test")

    assert scheduler.settled == []
    assert scheduler.restarted == 1
    assert scheduler.failed == 0


def test_unclassified_failure_retries_folder_generation() -> None:
    scheduler = _FolderScheduler(retry_results=[1])
    background = BackgroundServices(
        boot_id="boot-test",
        configs=object(),  # type: ignore[arg-type]
        scheduler=scheduler,  # type: ignore[arg-type]
        worker=_FailingWorker(RuntimeError("provider unavailable")),  # type: ignore[arg-type]
        apply=object(),  # type: ignore[arg-type]
    )

    background._execute_job("job-test", "run-test")

    assert scheduler.settled == []
    assert scheduler.restarted == 0
    assert scheduler.retry_calls == [("run-test", 3)]
    assert scheduler.failed == 0


def test_unclassified_failure_moves_to_fail_after_three_retries() -> None:
    scheduler = _FolderScheduler(retry_results=[None])
    dispositions = _FolderDispositions()
    background = BackgroundServices(
        boot_id="boot-test",
        configs=_ManualConfigs(),  # type: ignore[arg-type]
        scheduler=scheduler,  # type: ignore[arg-type]
        worker=_FailingWorker(RuntimeError("provider unavailable")),  # type: ignore[arg-type]
        apply=object(),  # type: ignore[arg-type]
        folder_dispositions=dispositions,  # type: ignore[arg-type]
    )

    background._execute_job("job-test", "run-test")

    assert scheduler.retry_calls == [("run-test", 3)]
    assert dispositions.failures == [
        ("run-test", "agent_retry_exhausted")
    ]
    assert scheduler.failed == 1
    assert scheduler.settled == [True]
