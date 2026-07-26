from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from reeloom.server.background import BackgroundServices
from reeloom.server.config import ApplyPolicy
from reeloom.server.errors import ServerError, ServerErrorCode


class _UnavailableConfigs:
    def head(self) -> None:
        raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE)


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


@pytest.mark.parametrize(
    ("worker", "configs", "apply"),
    (
        (_UnavailableWorker(), object(), object()),
        (_SuccessfulWorker(), _AutomaticConfigs(), _UnavailableApply()),
    ),
)
def test_database_failure_is_rethrown_after_job_is_settled(
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
    assert scheduler.settled == [False]
