from __future__ import annotations

import errno
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import reeloom.executor.apply as apply_module
from reeloom.adapters.approval import FilesystemApprovalStore
from reeloom.adapters.filesystem import (
    FilesystemPlanCompiler,
    FilesystemScanner,
)
from reeloom.adapters.journal import FilesystemJournalStore
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.executor.apply import (
    ApplyStatus,
    FilesystemExecutor,
)
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.manifest import ExecutionManifest
from reeloom.executor.preflight import FilesystemPreflightExecutor
from reeloom.executor.transaction import TransactionRecord
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.naming import SeriesIdentity
from reeloom.kernel.rename_plan import RenamePlan
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot

_NOW = datetime(2026, 7, 24, 15, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Environment:
    source: Path
    output: Path
    plan: RenamePlan
    approval: ApprovalRecord
    plans: FilesystemPlanStore
    approvals: FilesystemApprovalStore
    journals: FilesystemJournalStore

    def executor(self) -> FilesystemExecutor:
        return FilesystemExecutor(
            plans=self.plans,
            approvals=self.approvals,
            journals=self.journals,
        )


def _setup(
    tmp_path: Path,
    *,
    mapped_count: int = 2,
) -> _Environment:
    source = tmp_path / "incoming"
    output = tmp_path / "anime"
    plan_store_path = tmp_path / "plans"
    approval_store_path = tmp_path / "approvals"
    journal_path = tmp_path / "journals"
    for path in (
        source,
        output,
        plan_store_path,
        approval_store_path,
        journal_path,
    ):
        path.mkdir()
    for number in range(1, mapped_count + 1):
        (source / f"episode-{number}.mkv").write_bytes(
            f"video-{number}".encode()
        )
    (source / "unmapped.mkv").write_bytes(b"unmapped")

    scan = FilesystemScanner().scan(AuthorizedRoot.create(source))
    mapping = MappingDraft.from_dict(
        {
            "videos": [
                {
                    "video_id": f"video:{number}",
                    "season": 1,
                    "episode_start": number,
                    "episode_end": number,
                }
                for number in range(1, mapped_count + 1)
            ],
            "subtitles": [],
        },
        candidates=scan.snapshot.candidates,
        catalog=EpisodeCatalog.from_counts({1: 12}),
    )
    plan = FilesystemPlanCompiler(
        scan=scan,
        output_root=AuthorizedRoot.create(output),
    ).compile(
        run_id="run-m6-apply",
        work_type=TmdbWorkType.ANIME,
        series=SeriesIdentity(
            title_zh_cn="正确动画",
            year=2024,
            tmdb_id=200,
        ),
        mapping=mapping,
        subtitle_variants=(),
        created_at=_NOW,
    )
    plans = FilesystemPlanStore(
        AuthorizedRoot.create(plan_store_path)
    )
    plans.save(plan)
    approval = ApprovalRecord.create(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        scope=ApprovalScope.APPLY,
        expires_at=_NOW + timedelta(minutes=5),
        nonce="a" * 32,
    )
    approvals = FilesystemApprovalStore(
        AuthorizedRoot.create(approval_store_path),
        clock=lambda: _NOW,
    )
    approvals.issue(approval)
    return _Environment(
        source=source,
        output=output,
        plan=plan,
        approval=approval,
        plans=plans,
        approvals=approvals,
        journals=FilesystemJournalStore(
            AuthorizedRoot.create(journal_path)
        ),
    )


def _apply(environment: _Environment):
    return environment.executor().apply(
        plan_hash=environment.plan.plan_hash,
        approval_id=environment.approval.approval_id,
    )


def _begin_legacy_claimed_transaction(
    environment: _Environment,
) -> TransactionRecord:
    manifest = ExecutionManifest.from_canonical_bytes(
        environment.plans.load(environment.plan.plan_hash),
        plan_hash=environment.plan.plan_hash,
    )
    transaction = TransactionRecord.create(
        manifest,
        approval_id=environment.approval.approval_id,
    )
    environment.journals.begin(transaction)
    environment.approvals.claim(
        approval_id=environment.approval.approval_id,
        run_id=environment.plan.run_id,
        plan_hash=environment.plan.plan_hash,
        scope=ApprovalScope.APPLY,
    )
    return transaction


def _destination(
    environment: _Environment,
    move_index: int,
) -> Path:
    return environment.output / Path(
        environment.plan.draft.moves[move_index].destination
    )


def test_apply_moves_only_mapped_files_after_journal_is_durable(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path)

    result = _apply(environment)

    assert result.status is ApplyStatus.COMPLETED
    assert result.applied_count == 2
    assert result.rolled_back_count == 0
    assert result.failure_code is None
    assert not (environment.source / "episode-1.mkv").exists()
    assert not (environment.source / "episode-2.mkv").exists()
    assert _destination(environment, 0).read_bytes() == b"video-1"
    assert _destination(environment, 1).read_bytes() == b"video-2"
    assert (
        environment.source / "unmapped.mkv"
    ).read_bytes() == b"unmapped"
    journal_names = {
        path.name for path in environment.journals.root.path.iterdir()
    }
    assert any(name.endswith(".journal.json") for name in journal_names)
    assert any(name.endswith(".completed.json") for name in journal_names)
    journal = next(
        path
        for path in environment.journals.root.path.iterdir()
        if path.name.endswith(".journal.json")
    )
    rollback = json.loads(journal.read_bytes())["rollback_moves"]
    assert [
        item["candidate_id"] for item in rollback
    ] == ["video:2", "video:1"]


def test_rollback_manifest_exists_before_first_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    real_rename = apply_module._rename_noreplace
    observed = False

    def assert_journal_then_rename(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal observed
        observed = any(
            path.name.endswith(".journal.json")
            for path in environment.journals.root.path.iterdir()
        )
        assert observed
        real_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )

    monkeypatch.setattr(
        apply_module,
        "_rename_noreplace",
        assert_journal_then_rename,
    )

    result = _apply(environment)

    assert observed
    assert result.status is ApplyStatus.COMPLETED


def test_unsupported_atomic_move_resumes_with_same_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)

    def unsupported(*args: object) -> None:
        del args
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(
        apply_module,
        "_rename_noreplace",
        unsupported,
    )
    with pytest.raises(ExecutorError) as raised:
        _apply(environment)

    assert (
        raised.value.code
        is ExecutorErrorCode.ATOMIC_MOVE_UNSUPPORTED
    )
    assert (environment.source / "episode-1.mkv").is_file()
    assert not _destination(environment, 0).exists()

    monkeypatch.undo()
    recovered = environment.executor().recover(
        plan_hash=environment.plan.plan_hash,
        approval_id=environment.approval.approval_id,
    )

    assert recovered.status is ApplyStatus.COMPLETED
    assert recovered.failure_code is (
        ExecutorErrorCode.ATOMIC_MOVE_UNSUPPORTED
    )
    assert _destination(environment, 0).is_file()


def test_permission_denied_resumes_with_same_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)

    def denied(*args: object) -> None:
        del args
        raise OSError(errno.EACCES, "untrusted backend text")

    monkeypatch.setattr(
        apply_module,
        "_rename_noreplace",
        denied,
    )
    with pytest.raises(ExecutorError) as raised:
        _apply(environment)

    assert raised.value.code is ExecutorErrorCode.PERMISSION_DENIED
    assert (environment.source / "episode-1.mkv").is_file()
    assert not _destination(environment, 0).exists()

    monkeypatch.undo()
    recovered = environment.executor().recover(
        plan_hash=environment.plan.plan_hash,
        approval_id=environment.approval.approval_id,
    )

    assert recovered.status is ApplyStatus.COMPLETED
    assert recovered.failure_code is ExecutorErrorCode.PERMISSION_DENIED
    assert _destination(environment, 0).is_file()


def test_error_after_move_is_reconciled_without_duplicate_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    real_rename = apply_module._rename_noreplace

    def moved_then_error(*args: object) -> None:
        real_rename(*args)
        raise OSError(errno.EIO, "late error")

    monkeypatch.setattr(
        apply_module,
        "_rename_noreplace",
        moved_then_error,
    )

    result = _apply(environment)

    assert result.status is ApplyStatus.COMPLETED
    assert result.applied_count == 1
    assert _destination(environment, 0).is_file()


def test_apply_never_overwrites_target_created_at_rename_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    destination = _destination(environment, 0)
    real_rename = apply_module._rename_noreplace
    raced = False

    def create_target_then_rename(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal raced
        if not raced:
            raced = True
            destination.write_bytes(b"racer")
        real_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )

    monkeypatch.setattr(
        apply_module,
        "_rename_noreplace",
        create_target_then_rename,
    )

    result = _apply(environment)

    assert raced
    assert result.status is ApplyStatus.ROLLED_BACK
    assert (
        result.failure_code
        is ExecutorErrorCode.DESTINATION_COLLISION
    )
    assert destination.read_bytes() == b"racer"
    assert (
        environment.source / "episode-1.mkv"
    ).read_bytes() == b"video-1"


def test_partial_failure_rolls_back_applied_moves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path)
    real_rename = apply_module._rename_noreplace
    calls = 0

    def fail_second_forward_rename(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "injected move failure")
        real_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )

    monkeypatch.setattr(
        apply_module,
        "_rename_noreplace",
        fail_second_forward_rename,
    )

    result = _apply(environment)

    assert result.status is ApplyStatus.ROLLED_BACK
    assert result.applied_count == 1
    assert result.rolled_back_count == 1
    assert result.failure_code is ExecutorErrorCode.TRANSIENT_IO
    assert (
        environment.source / "episode-1.mkv"
    ).read_bytes() == b"video-1"
    assert (
        environment.source / "episode-2.mkv"
    ).read_bytes() == b"video-2"
    assert not _destination(environment, 0).exists()
    assert not _destination(environment, 1).exists()
    assert (
        environment.source / "unmapped.mkv"
    ).read_bytes() == b"unmapped"


def test_recovery_rolls_back_crash_after_rename_before_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)

    def crash_before_move_event(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(
        FilesystemJournalStore,
        "record_move",
        crash_before_move_event,
    )
    with pytest.raises(KeyboardInterrupt):
        _apply(environment)
    monkeypatch.undo()
    assert not (environment.source / "episode-1.mkv").exists()
    assert _destination(environment, 0).read_bytes() == b"video-1"

    recovered = environment.executor().recover(
        plan_hash=environment.plan.plan_hash,
        approval_id=environment.approval.approval_id,
    )
    recovered_again = environment.executor().recover(
        plan_hash=environment.plan.plan_hash,
        approval_id=environment.approval.approval_id,
    )

    assert recovered.status is ApplyStatus.ROLLED_BACK
    assert (
        environment.source / "episode-1.mkv"
    ).read_bytes() == b"video-1"
    assert not _destination(environment, 0).exists()
    assert recovered.rolled_back_count == 1
    assert recovered_again.status is ApplyStatus.ROLLED_BACK
    assert recovered_again.applied_count == 1
    assert recovered_again.rolled_back_count == 1
    assert (
        environment.source / "episode-1.mkv"
    ).read_bytes() == b"video-1"
    assert not _destination(environment, 0).exists()


def test_recovery_refuses_to_overwrite_recreated_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)

    def crash_before_move_event(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(
        FilesystemJournalStore,
        "record_move",
        crash_before_move_event,
    )
    with pytest.raises(KeyboardInterrupt):
        _apply(environment)
    monkeypatch.undo()
    source = environment.source / "episode-1.mkv"
    source.write_bytes(b"new-source")

    with pytest.raises(ExecutorError) as raised:
        environment.executor().recover(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )

    assert raised.value.code is ExecutorErrorCode.RECOVERY_REQUIRED
    assert raised.value.context == {
        "candidate_id": "video:1",
        "destination_relative_path": (
            environment.plan.draft.moves[0].destination.as_posix()
        ),
        "destination_state": "expected",
        "source_relative_path": "episode-1.mkv",
        "source_state": "drifted_regular",
    }
    assert source.read_bytes() == b"new-source"
    assert _destination(environment, 0).read_bytes() == b"video-1"


def test_recovery_is_idempotent_after_crash_during_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path)
    real_rename = apply_module._rename_noreplace
    calls = 0

    def fail_second_forward_rename(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "injected move failure")
        real_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )

    def crash_after_rollback_rename(
        *args: object,
        **kwargs: object,
    ) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(
        apply_module,
        "_rename_noreplace",
        fail_second_forward_rename,
    )
    monkeypatch.setattr(
        FilesystemJournalStore,
        "record_rollback",
        crash_after_rollback_rename,
    )
    with pytest.raises(KeyboardInterrupt):
        _apply(environment)
    monkeypatch.undo()

    recovered = environment.executor().recover(
        plan_hash=environment.plan.plan_hash,
        approval_id=environment.approval.approval_id,
    )

    assert recovered.status is ApplyStatus.ROLLED_BACK
    assert recovered.applied_count == 1
    assert recovered.rolled_back_count == 1
    assert recovered.failure_code is ExecutorErrorCode.TRANSIENT_IO
    assert (
        environment.source / "episode-1.mkv"
    ).read_bytes() == b"video-1"
    assert (
        environment.source / "episode-2.mkv"
    ).read_bytes() == b"video-2"
    assert not _destination(environment, 0).exists()
    assert not _destination(environment, 1).exists()


def test_recovery_cannot_race_live_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path)
    move_recorded = threading.Event()
    continue_apply = threading.Event()
    real_record_move = FilesystemJournalStore.record_move
    calls = 0

    def pause_after_first_move(
        store: FilesystemJournalStore,
        transaction: TransactionRecord,
        candidate_id: CandidateId,
    ) -> None:
        nonlocal calls
        real_record_move(store, transaction, candidate_id)
        calls += 1
        if calls == 1:
            move_recorded.set()
            assert continue_apply.wait(timeout=5)

    monkeypatch.setattr(
        FilesystemJournalStore,
        "record_move",
        pause_after_first_move,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_apply, environment)
        assert move_recorded.wait(timeout=5)
        try:
            with pytest.raises(ExecutorError) as raised:
                environment.executor().recover(
                    plan_hash=environment.plan.plan_hash,
                    approval_id=environment.approval.approval_id,
                )
            assert (
                raised.value.code
                is ExecutorErrorCode.TRANSACTION_BUSY
            )
        finally:
            continue_apply.set()
        result = future.result(timeout=5)

    assert result.status is ApplyStatus.COMPLETED
    assert not (environment.source / "episode-1.mkv").exists()
    assert not (environment.source / "episode-2.mkv").exists()
    assert _destination(environment, 0).read_bytes() == b"video-1"
    assert _destination(environment, 1).read_bytes() == b"video-2"


def test_recovery_rejects_conflicting_terminal_events(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    result = _apply(environment)
    manifest = ExecutionManifest.from_canonical_bytes(
        environment.plans.load(environment.plan.plan_hash),
        plan_hash=environment.plan.plan_hash,
    )
    transaction = TransactionRecord.create(
        manifest,
        approval_id=environment.approval.approval_id,
    )
    environment.journals.record_rolled_back(transaction)

    with pytest.raises(ExecutorError) as raised:
        environment.executor().recover(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )

    assert raised.value.code is ExecutorErrorCode.RECOVERY_REQUIRED
    assert result.status is ApplyStatus.COMPLETED
    assert not (environment.source / "episode-1.mkv").exists()
    assert _destination(environment, 0).read_bytes() == b"video-1"


def test_apply_does_not_finalize_when_current_move_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    source = environment.source / "episode-1.mkv"
    destination = _destination(environment, 0)
    real_rename = apply_module._rename_noreplace
    raced = False

    def recreate_source_after_forward_rename(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal raced
        real_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )
        if not raced:
            raced = True
            source.write_bytes(b"new-source")

    monkeypatch.setattr(
        apply_module,
        "_rename_noreplace",
        recreate_source_after_forward_rename,
    )

    with pytest.raises(ExecutorError) as raised:
        _apply(environment)

    assert raised.value.code is ExecutorErrorCode.RECOVERY_REQUIRED
    assert source.read_bytes() == b"new-source"
    assert destination.read_bytes() == b"video-1"
    assert not any(
        path.name.endswith(".rolled-back.json")
        for path in environment.journals.root.path.iterdir()
    )
    with pytest.raises(ExecutorError) as recovery:
        environment.executor().recover(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )
    assert recovery.value.code is ExecutorErrorCode.RECOVERY_REQUIRED


def test_apply_never_restores_an_unexpected_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    source = environment.source / "episode-1.mkv"
    destination = _destination(environment, 0)
    displaced = tmp_path / "displaced-original.mkv"
    real_rename = apply_module._rename_noreplace
    raced = False

    def replace_destination_after_forward_rename(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal raced
        real_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )
        if not raced:
            raced = True
            destination.rename(displaced)
            destination.write_bytes(b"racer")

    monkeypatch.setattr(
        apply_module,
        "_rename_noreplace",
        replace_destination_after_forward_rename,
    )

    with pytest.raises(ExecutorError) as raised:
        _apply(environment)

    assert raised.value.code is ExecutorErrorCode.RECOVERY_REQUIRED
    assert not source.exists()
    assert destination.read_bytes() == b"racer"
    assert displaced.read_bytes() == b"video-1"


def test_completed_write_uncertainty_never_triggers_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    real_record_completed = FilesystemJournalStore.record_completed

    def persist_completed_then_fail(
        store: FilesystemJournalStore,
        transaction: TransactionRecord,
    ) -> None:
        real_record_completed(store, transaction)
        raise ExecutorError(ExecutorErrorCode.JOURNAL_FAILURE)

    monkeypatch.setattr(
        FilesystemJournalStore,
        "record_completed",
        persist_completed_then_fail,
    )

    with pytest.raises(ExecutorError) as raised:
        _apply(environment)

    assert raised.value.code is ExecutorErrorCode.RECOVERY_REQUIRED
    assert not (environment.source / "episode-1.mkv").exists()
    assert _destination(environment, 0).read_bytes() == b"video-1"
    assert not any(
        path.name.endswith(".rolled-back.json")
        for path in environment.journals.root.path.iterdir()
    )

    monkeypatch.undo()
    recovered = environment.executor().recover(
        plan_hash=environment.plan.plan_hash,
        approval_id=environment.approval.approval_id,
    )
    assert recovered.status is ApplyStatus.COMPLETED


def test_journal_is_durable_before_approval_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    real_claim = FilesystemApprovalStore.claim
    observed = False

    def assert_journal_then_claim(
        store: FilesystemApprovalStore,
        **kwargs: object,
    ) -> ApprovalRecord:
        nonlocal observed
        observed = any(
            path.name.endswith(".journal.json")
            for path in environment.journals.root.path.iterdir()
        )
        assert observed
        return real_claim(store, **kwargs)

    monkeypatch.setattr(
        FilesystemApprovalStore,
        "claim",
        assert_journal_then_claim,
    )

    result = _apply(environment)

    assert observed
    assert result.status is ApplyStatus.COMPLETED


def test_recovery_handles_crash_after_claim_before_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    real_validate = FilesystemPreflightExecutor.validate
    calls = 0

    def crash_after_claim(
        preflight: FilesystemPreflightExecutor,
        manifest: ExecutionManifest,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            real_validate(preflight, manifest)
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(
        FilesystemPreflightExecutor,
        "validate",
        crash_after_claim,
    )
    with pytest.raises(KeyboardInterrupt):
        _apply(environment)
    monkeypatch.undo()

    recovered = environment.executor().recover(
        plan_hash=environment.plan.plan_hash,
        approval_id=environment.approval.approval_id,
    )

    assert recovered.status is ApplyStatus.ROLLED_BACK


def test_source_drift_before_claim_leaves_no_transaction_state(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    source = environment.source / "episode-1.mkv"
    source.rename(environment.source / "original-episode-1.mkv")
    source.write_bytes(b"replacement")

    with pytest.raises(ExecutorError) as raised:
        _apply(environment)

    assert raised.value.code is ExecutorErrorCode.SOURCE_DRIFT
    assert raised.value.context["candidate_id"] == "video:1"
    assert not any(
        path.name.endswith(".claim.json")
        for path in environment.approvals.root.path.iterdir()
    )
    assert tuple(environment.journals.root.path.iterdir()) == ()


def test_source_drift_after_claim_is_settled_without_moves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    real_validate = FilesystemPreflightExecutor.validate
    calls = 0

    def drift_after_claim(
        preflight: FilesystemPreflightExecutor,
        manifest: ExecutionManifest,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            source = environment.source / "episode-1.mkv"
            source.rename(environment.source / "original-episode-1.mkv")
            source.write_bytes(b"replacement")
        real_validate(preflight, manifest)

    monkeypatch.setattr(
        FilesystemPreflightExecutor,
        "validate",
        drift_after_claim,
    )

    result = _apply(environment)

    assert result.status is ApplyStatus.ROLLED_BACK
    assert result.failure_code is ExecutorErrorCode.SOURCE_DRIFT
    assert result.applied_count == 0
    assert result.rolled_back_count == 0
    assert not _destination(environment, 0).exists()
    assert any(
        path.name.endswith(".claim.json")
        for path in environment.approvals.root.path.iterdir()
    )


def test_legacy_claimed_source_drift_with_absent_destination_settles(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    transaction = _begin_legacy_claimed_transaction(environment)
    source = environment.source / "episode-1.mkv"
    source.rename(environment.source / "original-episode-1.mkv")
    source.write_bytes(b"replacement")

    recovered = environment.executor().recover(
        plan_hash=environment.plan.plan_hash,
        approval_id=environment.approval.approval_id,
    )
    recovered_again = environment.executor().recover(
        plan_hash=environment.plan.plan_hash,
        approval_id=environment.approval.approval_id,
    )

    assert recovered.status is ApplyStatus.ROLLED_BACK
    assert recovered.failure_code is None
    assert recovered.applied_count == 0
    assert recovered.rolled_back_count == 0
    assert recovered_again == recovered
    assert environment.journals.is_rolled_back(transaction)
    assert source.read_bytes() == b"replacement"
    assert not _destination(environment, 0).exists()


def test_legacy_claimed_recovery_rejects_missing_source_and_destination(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    _begin_legacy_claimed_transaction(environment)
    (environment.source / "episode-1.mkv").unlink()

    with pytest.raises(ExecutorError) as raised:
        environment.executor().recover(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )

    assert raised.value.code is ExecutorErrorCode.RECOVERY_REQUIRED
    assert raised.value.context == {
        "candidate_id": "video:1",
        "destination_relative_path": (
            environment.plan.draft.moves[0].destination.as_posix()
        ),
        "destination_state": "absent",
        "source_relative_path": "episode-1.mkv",
        "source_state": "absent",
    }


def test_legacy_claimed_recovery_rejects_nonregular_source(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)
    _begin_legacy_claimed_transaction(environment)
    source = environment.source / "episode-1.mkv"
    source.rename(environment.source / "original-episode-1.mkv")
    source.mkdir()

    with pytest.raises(ExecutorError) as raised:
        environment.executor().recover(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )

    assert raised.value.code is ExecutorErrorCode.RECOVERY_REQUIRED
    assert raised.value.context["source_state"] == "other"
    assert raised.value.context["destination_state"] == "absent"
    assert source.is_dir()
    assert not _destination(environment, 0).exists()


def test_recovery_does_not_settle_drift_after_recorded_move_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path, mapped_count=1)

    def crash_before_completed_event(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(
        FilesystemJournalStore,
        "record_completed",
        crash_before_completed_event,
    )
    with pytest.raises(KeyboardInterrupt):
        _apply(environment)
    monkeypatch.undo()
    destination = _destination(environment, 0)
    destination.unlink()
    source = environment.source / "episode-1.mkv"
    source.write_bytes(b"replacement")

    with pytest.raises(ExecutorError) as raised:
        environment.executor().recover(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )

    assert raised.value.code is ExecutorErrorCode.RECOVERY_REQUIRED
    assert raised.value.context["source_state"] == "drifted_regular"
    assert raised.value.context["destination_state"] == "absent"
    assert source.read_bytes() == b"replacement"
    assert not destination.exists()
