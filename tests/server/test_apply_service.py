from __future__ import annotations

from reeloom.server.apply_service import ApplyCoordinator


class _Layouts:
    def settlement_for_plan(self, **kwargs: object) -> None:
        del kwargs
        return None

    def settlement(self, **kwargs: object) -> None:
        del kwargs
        return None


class _Executor:
    def recover(self, **kwargs: object) -> object:
        del kwargs
        raise AssertionError("read-only resolution invoked recovery")


def test_resolution_never_retries_filesystem_effects() -> None:
    coordinator = ApplyCoordinator(
        pool=object(),  # type: ignore[arg-type]
        approvals=object(),  # type: ignore[arg-type]
        executor=_Executor(),  # type: ignore[arg-type]
        completed_layouts=_Layouts(),  # type: ignore[arg-type]
    )

    assert coordinator.resolve(
        run_id="run-1",
        plan_hash="sha256:" + "a" * 64,
    ) is None
    assert coordinator.resolve(
        run_id="run-1",
        plan_hash="sha256:" + "a" * 64,
        approval_id="approval-1",
    ) is None
