from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from reeloom.executor.apply import ApplyResult, ApplyStatus
from reeloom.executor.errors import ExecutorErrorCode
from reeloom.executor.manifest import (
    ExecutionManifest,
    ExecutionMove,
)
from reeloom.kernel.amendment import (
    CompletedLayout,
    CompletedLayoutFile,
)
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.rename_plan import RootBinding
from reeloom.server import apply_service
from reeloom.server.apply_service import ApplyCoordinator
from reeloom.server.completed_layout import (
    PostgresCompletedLayoutRepository,
)
from reeloom.server.errors import ServerError, ServerErrorCode


_PLAN_HASH = "sha256:" + "a" * 64
_TRANSACTION_ID = "txn-v1-" + "b" * 64
_APPROVAL_ID = "approval-v1-" + "c" * 64


class _Layouts:
    def __init__(self) -> None:
        self.appended: tuple[ApplyResult, object | None] | None = None

    def settlement_for_plan(self, **kwargs: object) -> None:
        del kwargs
        return None

    def settlement(self, **kwargs: object) -> None:
        del kwargs
        return None

    def settle_and_append(
        self,
        *,
        result: ApplyResult,
        layout: object | None,
    ) -> None:
        self.appended = (result, layout)


class _Plans:
    def load(self, plan_hash: str) -> bytes:
        assert plan_hash == _PLAN_HASH
        return b"canonical-plan"


class _Executor:
    plans = _Plans()

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
        plan_hash=_PLAN_HASH,
    ) is None
    assert coordinator.resolve(
        run_id="run-1",
        plan_hash=_PLAN_HASH,
        approval_id="approval-1",
    ) is None


@pytest.mark.parametrize(
    ("same_root", "has_move", "expects_layout"),
    (
        (False, False, False),
        (True, False, True),
        (False, True, True),
    ),
    ids=("empty-initial", "empty-amendment", "nonempty-initial"),
)
def test_completed_settlement_only_omits_empty_initial_layout(
    monkeypatch: pytest.MonkeyPatch,
    *,
    same_root: bool,
    has_move: bool,
    expects_layout: bool,
) -> None:
    source = RootBinding(PurePosixPath("/incoming"), 1, 2)
    output = source if same_root else RootBinding(
        PurePosixPath("/library"), 1, 3
    )
    candidate_id = CandidateId(CandidateKind.VIDEO, 1)
    manifest = ExecutionManifest(
        plan_hash=_PLAN_HASH,
        run_id="run-1",
        source_root=source,
        output_root=output,
        sources=(),
        moves=(
            (
                ExecutionMove(
                    source_id=candidate_id,
                    video_id=candidate_id,
                    destination=PurePosixPath(
                        "Series/S01/Series - S01E01.mkv"
                    ),
                ),
            )
            if has_move
            else ()
        ),
    )
    monkeypatch.setattr(
        ExecutionManifest,
        "from_canonical_bytes",
        classmethod(lambda cls, content, *, plan_hash: manifest),
    )
    captured_layout = object()
    captures: list[tuple[ExecutionManifest, str]] = []

    def capture(
        value: ExecutionManifest,
        *,
        transaction_id: str,
    ) -> object:
        captures.append((value, transaction_id))
        return captured_layout

    monkeypatch.setattr(apply_service, "capture_completed_layout", capture)
    layouts = _Layouts()
    coordinator = ApplyCoordinator(
        pool=object(),  # type: ignore[arg-type]
        approvals=object(),  # type: ignore[arg-type]
        executor=_Executor(),  # type: ignore[arg-type]
        completed_layouts=layouts,  # type: ignore[arg-type]
    )
    result = ApplyResult(
        transaction_id=_TRANSACTION_ID,
        plan_hash=_PLAN_HASH,
        approval_id=_APPROVAL_ID,
        status=ApplyStatus.COMPLETED,
        applied_count=1 if has_move else 0,
        rolled_back_count=0,
        failure_code=None,
    )

    coordinator._settle(result)

    assert layouts.appended == (
        result,
        captured_layout if expects_layout else None,
    )
    assert len(captures) == int(expects_layout)


def _layout() -> CompletedLayout:
    candidate_id = CandidateId(CandidateKind.VIDEO, 1)
    return CompletedLayout(
        run_id="run-1",
        original_plan_hash=_PLAN_HASH,
        transaction_id=_TRANSACTION_ID,
        root=RootBinding(PurePosixPath("/library"), 1, 3),
        files=(
            CompletedLayoutFile(
                candidate_id=candidate_id,
                kind=CandidateKind.VIDEO,
                relative_path=PurePosixPath(
                    "Series/S01/Series - S01E01.mkv"
                ),
                size_bytes=1,
                device=1,
                inode=4,
                mtime_ns=5,
                ctime_ns=6,
                sample_digest=None,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("result", "layout"),
    (
        (
            ApplyResult(
                _TRANSACTION_ID,
                _PLAN_HASH,
                _APPROVAL_ID,
                ApplyStatus.COMPLETED,
                1,
                0,
                None,
            ),
            None,
        ),
        (
            ApplyResult(
                _TRANSACTION_ID,
                _PLAN_HASH,
                _APPROVAL_ID,
                ApplyStatus.COMPLETED,
                0,
                1,
                None,
            ),
            None,
        ),
        (
            ApplyResult(
                _TRANSACTION_ID,
                _PLAN_HASH,
                _APPROVAL_ID,
                ApplyStatus.COMPLETED,
                0,
                0,
                ExecutorErrorCode.MOVE_FAILED,
            ),
            None,
        ),
        (
            ApplyResult(
                _TRANSACTION_ID,
                _PLAN_HASH,
                _APPROVAL_ID,
                ApplyStatus.ROLLED_BACK,
                1,
                1,
                ExecutorErrorCode.MOVE_FAILED,
            ),
            _layout(),
        ),
    ),
    ids=(
        "completed-applied",
        "completed-rolled-back",
        "completed-failure",
        "rolled-back-layout",
    ),
)
def test_repository_rejects_invalid_optional_layout_pair(
    result: ApplyResult,
    layout: CompletedLayout | None,
) -> None:
    repository = PostgresCompletedLayoutRepository(
        object(),  # type: ignore[arg-type]
    )

    with pytest.raises(ServerError) as raised:
        repository.settle_and_append(result=result, layout=layout)

    assert raised.value.code is ServerErrorCode.INTERACTION_CONFLICT
