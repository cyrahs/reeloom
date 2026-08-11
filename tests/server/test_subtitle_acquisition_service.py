from __future__ import annotations

import inspect

from reeloom.server.subtitle_acquisition_service import (
    SubtitleAcquisitionCoordinator,
)


def test_v2_coordinator_constructor_has_only_shared_operation_dependencies() -> None:
    parameters = inspect.signature(
        SubtitleAcquisitionCoordinator
    ).parameters

    assert "operation_approvals" in parameters
    assert "operations" in parameters
    assert "approvals" not in parameters
    assert "successors" not in parameters
    assert "publications" not in parameters


def test_v2_coordinator_has_no_user_recovery_commands() -> None:
    assert not hasattr(
        SubtitleAcquisitionCoordinator, "retry_blocked_and_execute"
    )
    assert not hasattr(SubtitleAcquisitionCoordinator, "fail_blocked")
    assert not hasattr(SubtitleAcquisitionCoordinator, "resolve_failed")


def test_automatic_reconciler_is_explicitly_scoped_to_forward_v2() -> None:
    source = inspect.getsource(
        SubtitleAcquisitionCoordinator.reconcile_approved
    )

    assert "control.mode = 'forward_v2'" in source
    assert "terminal.run_id IS NULL" in source
