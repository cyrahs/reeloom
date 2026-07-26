from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

import reeloom.executor.apply as apply_module
from reeloom.adapters.approval import FilesystemApprovalStore
from reeloom.adapters.journal import FilesystemJournalStore
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.executor.apply import ApplyStatus, FilesystemExecutor
from reeloom.executor.errors import ExecutorErrorCode
from reeloom.kernel.amendment import (
    CompletedLayout,
    CompletedLayoutFile,
    DesiredLayoutMove,
    compile_amendment,
)
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.rename_plan import RootBinding
from reeloom.policy.path_policy import AuthorizedRoot


def test_amendment_apply_keeps_original_on_destination_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive"
    plans_root = tmp_path / "plans"
    approvals_root = tmp_path / "approvals"
    journals_root = tmp_path / "journals"
    for root in (
        archive,
        plans_root,
        approvals_root,
        journals_root,
    ):
        root.mkdir()
    relative = PurePosixPath("Series/S01/Series - S01E01.mkv")
    source = archive / Path(relative)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original")
    identity = source.stat(follow_symlinks=False)
    bound = AuthorizedRoot.create(archive)
    candidate_id = CandidateId(CandidateKind.VIDEO, 1)
    layout = CompletedLayout(
        run_id="run-amendment",
        original_plan_hash="sha256:" + "a" * 64,
        transaction_id="txn-v1-" + "b" * 64,
        root=RootBinding(
            PurePosixPath(archive.as_posix()),
            bound.device,
            bound.inode,
        ),
        files=(
            CompletedLayoutFile(
                candidate_id=candidate_id,
                kind=CandidateKind.VIDEO,
                relative_path=relative,
                size_bytes=identity.st_size,
                device=identity.st_dev,
                inode=identity.st_ino,
                mtime_ns=identity.st_mtime_ns,
                ctime_ns=identity.st_ctime_ns,
                sample_digest=None,
            ),
        ),
    )
    destination = PurePosixPath("Series/S00/Series - S00E01.mkv")
    amendment = compile_amendment(
        layout=layout,
        desired=(
            DesiredLayoutMove(
                source_id=candidate_id,
                video_id=candidate_id,
                destination=destination,
                season=0,
                episode_start=1,
                episode_end=1,
            ),
        ),
        created_at=datetime(2026, 7, 25, tzinfo=UTC),
    )
    assert amendment is not None
    plans = FilesystemPlanStore(
        AuthorizedRoot.create(plans_root)
    )
    plans.save_amendment(amendment)
    now = datetime(2026, 7, 25, tzinfo=UTC)
    approval = ApprovalRecord.create(
        run_id=amendment.run_id,
        plan_hash=amendment.plan_hash,
        scope=ApprovalScope.APPLY,
        expires_at=now + timedelta(minutes=5),
        nonce="c" * 32,
    )
    approvals = FilesystemApprovalStore(
        AuthorizedRoot.create(approvals_root),
        clock=lambda: now,
    )
    approvals.issue(approval)
    raced = archive / Path(destination)
    raced.parent.mkdir(parents=True)
    real_rename = apply_module._rename_noreplace

    def race_then_rename(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        raced.write_bytes(b"racer")
        real_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )

    monkeypatch.setattr(
        apply_module,
        "_rename_noreplace",
        race_then_rename,
    )

    result = FilesystemExecutor(
        plans=plans,
        approvals=approvals,
        journals=FilesystemJournalStore(
            AuthorizedRoot.create(journals_root)
        ),
    ).apply(
        plan_hash=amendment.plan_hash,
        approval_id=approval.approval_id,
    )

    assert result.status is ApplyStatus.ROLLED_BACK
    assert result.failure_code is ExecutorErrorCode.DESTINATION_COLLISION
    assert source.read_bytes() == b"original"
    assert raced.read_bytes() == b"racer"
