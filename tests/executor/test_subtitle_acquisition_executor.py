from __future__ import annotations

import asyncio
import errno
import hashlib
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

import reeloom.executor.subtitle_acquisition as executor_module
from reeloom.adapters.approval import FilesystemApprovalStore
from reeloom.adapters.subtitle_journal import (
    FilesystemSubtitleAcquisitionJournalStore,
)
from reeloom.adapters.subtitle_plan_store import (
    FilesystemSubtitleAcquisitionPlanStore,
)
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.subtitle_acquisition import SubtitleAcquisitionExecutor
from reeloom.executor.subtitle_transaction import (
    SubtitleAcquisitionTransactionRecord,
)
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.rename_plan import RootBinding
from reeloom.kernel.subtitle_acquisition import (
    CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION,
    CURRENT_SUBTITLE_SEARCH_PARSER_VERSION,
    CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION,
    InspectedSubtitleMember,
    SubtitleAcquisitionPlan,
    SubtitleArchiveFormat,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleArchiveSource,
    SubtitleArchiveVolume,
    SubtitleReleaseId,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.subtitle_acquisition import (
    DownloadedArchiveVolume,
    DownloadedSubtitleArchiveSet,
    InspectedSubtitleArchiveSet,
)
from reeloom.server.watcher import NoFollowWatcher

_NOW = datetime(2026, 8, 4, tzinfo=UTC)
_ARCHIVE_CONTENT = b"PK\x03\x04fixed-archive"
_SUBTITLE_CONTENT = b"[Script Info]\nTitle: fixed\n"


@dataclass
class _FakeFetcher:
    workspace_root: Path
    source: SubtitleArchiveSource
    archive_path: Path
    drift_volume: bool = False
    calls: int = 0

    @property
    def provider_version(self) -> str:
        return CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION

    @property
    def parser_version(self) -> str:
        return CURRENT_SUBTITLE_SEARCH_PARSER_VERSION

    async def fetch(
        self,
        capability: SubtitleArchiveSetCapability,
    ) -> DownloadedSubtitleArchiveSet:
        self.calls += 1
        metadata = os.stat(self.archive_path, follow_symlinks=False)
        volume = self.source.volumes[0]
        if self.drift_volume:
            volume = replace(volume, sha256="f" * 64)
        return DownloadedSubtitleArchiveSet(
            capability,
            (
                DownloadedArchiveVolume(
                    volume,
                    self.archive_path,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                ),
            ),
        )


@dataclass
class _FakeInspector:
    source: SubtitleArchiveSource
    member: InspectedSubtitleMember
    manifest_drift: bool = False
    member_drift: bool = False
    extraction_drift: bool = False
    inspect_calls: int = 0
    extract_calls: int = 0

    @property
    def inspector_version(self) -> str:
        return CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION

    async def inspect(
        self,
        downloaded: DownloadedSubtitleArchiveSet,
        *,
        season_numbers: tuple[int, ...],
    ) -> InspectedSubtitleArchiveSet:
        self.inspect_calls += 1
        source = self.source
        member = self.member
        if self.manifest_drift:
            source = replace(source, manifest_digest="e" * 64)
        if self.member_drift:
            member = replace(member, sha256="d" * 64)
        return InspectedSubtitleArchiveSet(source, (member,), ())

    async def extract_member(self, downloaded, member) -> bytes:
        self.extract_calls += 1
        if self.extraction_drift:
            return b"different"
        return _SUBTITLE_CONTENT


@dataclass(frozen=True)
class _Environment:
    source_root: Path
    source_folder: Path
    plan: SubtitleAcquisitionPlan
    approval: ApprovalRecord
    plans: FilesystemSubtitleAcquisitionPlanStore
    approvals: FilesystemApprovalStore
    journals: FilesystemSubtitleAcquisitionJournalStore
    fetcher: _FakeFetcher
    inspector: _FakeInspector
    executor: SubtitleAcquisitionExecutor

    @property
    def transaction(self) -> SubtitleAcquisitionTransactionRecord:
        return SubtitleAcquisitionTransactionRecord.create(
            self.plan,
            approval_id=self.approval.approval_id,
        )


def _environment(tmp_path: Path) -> _Environment:
    source_root = tmp_path / "media"
    source_folder = source_root / "release"
    workspace = tmp_path / "workspace"
    plans_path = tmp_path / "plans"
    approvals_path = tmp_path / "approvals"
    journals_path = tmp_path / "journals"
    for path in (
        source_folder,
        workspace,
        plans_path,
        approvals_path,
        journals_path,
    ):
        path.mkdir(parents=True, exist_ok=True)
    archive_path = workspace / "attachment-34768.zip"
    archive_path.write_bytes(_ARCHIVE_CONTENT)
    (source_folder / "episode-01.mkv").write_bytes(b"fixed-video")
    root_stat = os.stat(source_root, follow_symlinks=False)
    folder_stat = os.stat(source_folder, follow_symlinks=False)
    candidate_snapshot_id = NoFollowWatcher().scan_folder(
        AuthorizedRoot.create(source_root),
        PurePosixPath(source_folder.name),
        logical_name=source_folder.name,
    ).candidates.snapshot_id
    archive_id = SubtitleArchiveSetId(1)
    volume = SubtitleArchiveVolume(
        1,
        34768,
        len(_ARCHIVE_CONTENT),
        hashlib.sha256(_ARCHIVE_CONTENT).hexdigest(),
    )
    source = SubtitleArchiveSource(
        SubtitleReleaseId(1),
        archive_id,
        SubtitleArchiveFormat.ZIP,
        (1,),
        10081,
        95257,
        "b" * 64,
        (volume,),
    )
    member = InspectedSubtitleMember(
        archive_id,
        PurePosixPath("Subs/E01.ass"),
        len(_SUBTITLE_CONTENT),
        hashlib.sha256(_SUBTITLE_CONTENT).hexdigest(),
    )
    plan = SubtitleAcquisitionPlan.create(
        run_id="run-m13-executor",
        config_revision_id="config-1",
        created_at=_NOW,
        source_root=RootBinding(
            PurePosixPath(source_root.as_posix()),
            root_stat.st_dev,
            root_stat.st_ino,
        ),
        source_folder=source_folder.name,
        source_folder_device=folder_stat.st_dev,
        source_folder_inode=folder_stat.st_ino,
        folder_generation_id="generation-1",
        candidate_snapshot_id=candidate_snapshot_id,
        tmdb_id=123,
        archives=(source,),
        inspected_members=(member,),
    )
    plans = FilesystemSubtitleAcquisitionPlanStore(
        AuthorizedRoot.create(plans_path)
    )
    approvals = FilesystemApprovalStore(
        AuthorizedRoot.create(approvals_path),
        clock=lambda: _NOW,
    )
    journals = FilesystemSubtitleAcquisitionJournalStore(
        AuthorizedRoot.create(journals_path)
    )
    approval = ApprovalRecord.create(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        scope=ApprovalScope.SUBTITLE_ACQUIRE,
        expires_at=_NOW + timedelta(minutes=15),
        nonce="n" * 32,
    )
    plans.save(plan)
    approvals.issue(approval)
    fetcher = _FakeFetcher(workspace, source, archive_path)
    inspector = _FakeInspector(source, member)
    executor = SubtitleAcquisitionExecutor(
        plans,
        approvals,
        journals,
        fetcher,
        inspector,
    )
    return _Environment(
        source_root,
        source_folder,
        plan,
        approval,
        plans,
        approvals,
        journals,
        fetcher,
        inspector,
        executor,
    )


def _simulated_native_rename(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    try:
        os.stat(
            destination_name,
            dir_fd=destination_parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(errno.EEXIST, "destination exists")
    os.rename(
        source_name,
        destination_name,
        src_dir_fd=source_parent_fd,
        dst_dir_fd=destination_parent_fd,
    )


def _claim_for_recovery(environment: _Environment) -> None:
    transaction = environment.transaction
    environment.journals.begin(transaction)
    environment.approvals.claim(
        approval_id=environment.approval.approval_id,
        run_id=environment.plan.run_id,
        plan_hash=environment.plan.plan_hash,
        scope=ApprovalScope.SUBTITLE_ACQUIRE,
    )
    environment.journals.record(transaction, "approval_claimed")


def test_apply_refetches_verifies_and_publishes_exact_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    monkeypatch.setattr(
        executor_module,
        "_native_rename_noreplace",
        _simulated_native_rename,
    )

    result = asyncio.run(
        environment.executor.apply(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )
    )

    destination = environment.source_folder / result.destination_name
    assert result.status == "completed"
    assert result.published_count == 1
    assert environment.fetcher.calls == 1
    assert environment.inspector.inspect_calls == 1
    assert environment.inspector.extract_calls == 1
    assert [item.name for item in destination.iterdir()] == [
        environment.plan.members[0].destination_name
    ]
    assert next(destination.iterdir()).read_bytes() == _SUBTITLE_CONTENT
    assert not (environment.source_folder / environment.transaction.staging_name).exists()
    assert environment.journals.has(environment.transaction, "completed")


@pytest.mark.parametrize("drift", ("volume", "manifest", "member"))
def test_apply_rejects_remote_or_manifest_drift_before_staging(
    tmp_path: Path,
    drift: str,
) -> None:
    environment = _environment(tmp_path)
    environment.fetcher.drift_volume = drift == "volume"
    environment.inspector.manifest_drift = drift == "manifest"
    environment.inspector.member_drift = drift == "member"

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(
            environment.executor.apply(
                plan_hash=environment.plan.plan_hash,
                approval_id=environment.approval.approval_id,
            )
        )

    assert raised.value.code is ExecutorErrorCode.SOURCE_DRIFT
    assert not (environment.source_folder / environment.transaction.staging_name).exists()
    assert not (environment.source_folder / environment.transaction.destination_name).exists()


def test_apply_rejects_extracted_member_drift_without_publishing(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    environment.inspector.extraction_drift = True

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(
            environment.executor.apply(
                plan_hash=environment.plan.plan_hash,
                approval_id=environment.approval.approval_id,
            )
        )

    assert raised.value.code is ExecutorErrorCode.SOURCE_DRIFT
    assert (environment.source_folder / environment.transaction.staging_name).is_dir()
    assert not (environment.source_folder / environment.transaction.destination_name).exists()


def test_apply_detects_source_identity_drift_before_network(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.source_folder.rename(environment.source_root / "release-old")
    environment.source_folder.mkdir()

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(
            environment.executor.apply(
                plan_hash=environment.plan.plan_hash,
                approval_id=environment.approval.approval_id,
            )
        )

    assert raised.value.code is ExecutorErrorCode.SOURCE_DRIFT
    assert environment.fetcher.calls == 0


def test_apply_detects_candidate_snapshot_drift_before_network(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    (environment.source_folder / "episode-01.mkv").write_bytes(
        b"changed-video"
    )

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(
            environment.executor.apply(
                plan_hash=environment.plan.plan_hash,
                approval_id=environment.approval.approval_id,
            )
        )

    assert raised.value.code is ExecutorErrorCode.SOURCE_DRIFT
    assert environment.fetcher.calls == 0


def test_destination_collision_is_fail_closed_before_network(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    collision = environment.source_folder / environment.transaction.destination_name
    collision.mkdir()
    marker = collision / "existing"
    marker.write_bytes(b"keep")

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(
            environment.executor.apply(
                plan_hash=environment.plan.plan_hash,
                approval_id=environment.approval.approval_id,
            )
        )

    assert raised.value.code is ExecutorErrorCode.DESTINATION_COLLISION
    assert raised.value.context == {
        "stage": "destination_preflight",
        "reason": "name_exists",
    }
    assert marker.read_bytes() == b"keep"
    assert environment.fetcher.calls == 0


def test_native_no_replace_unavailable_never_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)

    def unavailable(*args) -> None:
        raise OSError(errno.ENOSYS, "unsupported")

    monkeypatch.setattr(
        executor_module,
        "_native_rename_noreplace",
        unavailable,
    )

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(
            environment.executor.apply(
                plan_hash=environment.plan.plan_hash,
                approval_id=environment.approval.approval_id,
            )
        )

    assert raised.value.code is ExecutorErrorCode.ATOMIC_MOVE_UNSUPPORTED
    assert (environment.source_folder / environment.transaction.staging_name).is_dir()
    assert not (environment.source_folder / environment.transaction.destination_name).exists()


def test_recovery_adopts_empty_staging_created_after_started_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    transaction = environment.transaction
    _claim_for_recovery(environment)
    environment.journals.record(transaction, "staging_create_started")
    staging = environment.source_folder / transaction.staging_name
    staging.mkdir(mode=0o700)
    staging.chmod(0o755)
    monkeypatch.setattr(
        executor_module,
        "_native_rename_noreplace",
        _simulated_native_rename,
    )

    result = asyncio.run(
        environment.executor.recover(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )
    )

    assert result.status == "completed"
    assert environment.journals.staging_identity(transaction) is not None


def test_recovery_rejects_group_writable_unjournaled_staging(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    transaction = environment.transaction
    _claim_for_recovery(environment)
    environment.journals.record(transaction, "staging_create_started")
    staging = environment.source_folder / transaction.staging_name
    staging.mkdir(mode=0o700)
    staging.chmod(0o775)

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(
            environment.executor.recover(
                plan_hash=environment.plan.plan_hash,
                approval_id=environment.approval.approval_id,
            )
        )

    assert raised.value.code is ExecutorErrorCode.DESTINATION_COLLISION
    assert raised.value.context == {
        "stage": "staging_validate",
        "reason": "unsafe_permissions",
        "actual_mode": 0o775,
        "expected_policy": "owner_rwx_no_group_or_other_write",
    }


def test_recovery_accepts_exact_unjournaled_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    transaction = environment.transaction
    _claim_for_recovery(environment)
    environment.journals.record(transaction, "staging_create_started")
    staging = environment.source_folder / transaction.staging_name
    staging.mkdir(mode=0o700)
    metadata = os.stat(staging, follow_symlinks=False)
    environment.journals.record_staging(
        transaction,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )
    (staging / environment.plan.members[0].destination_name).write_bytes(
        _SUBTITLE_CONTENT
    )
    monkeypatch.setattr(
        executor_module,
        "_native_rename_noreplace",
        _simulated_native_rename,
    )

    asyncio.run(
        environment.executor.recover(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )
    )

    assert environment.inspector.extract_calls == 0
    assert environment.journals.has_member(transaction, 0)
    assert (environment.source_folder / transaction.destination_name).is_dir()


def test_recovery_reconciles_rename_before_published_journal_without_refetch(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    transaction = environment.transaction
    _claim_for_recovery(environment)
    environment.journals.record(transaction, "staging_create_started")
    staging = environment.source_folder / transaction.staging_name
    staging.mkdir(mode=0o700)
    member = staging / environment.plan.members[0].destination_name
    member.write_bytes(_SUBTITLE_CONTENT)
    metadata = os.stat(staging, follow_symlinks=False)
    environment.journals.record_staging(
        transaction,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )
    environment.journals.record_member(transaction, 0)
    environment.journals.record(transaction, "publish_started")
    root_fd = os.open(environment.source_folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _simulated_native_rename(
            root_fd,
            transaction.staging_name,
            root_fd,
            transaction.destination_name,
        )
    finally:
        os.close(root_fd)

    result = asyncio.run(
        environment.executor.recover(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )
    )

    assert result.status == "completed"
    assert environment.fetcher.calls == 0
    assert environment.journals.has(transaction, "published")
    assert environment.journals.has(transaction, "completed")


def test_corrupt_unjournaled_member_is_never_overwritten(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    transaction = environment.transaction
    _claim_for_recovery(environment)
    environment.journals.record(transaction, "staging_create_started")
    staging = environment.source_folder / transaction.staging_name
    staging.mkdir(mode=0o700)
    metadata = os.stat(staging, follow_symlinks=False)
    environment.journals.record_staging(
        transaction,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )
    member = staging / environment.plan.members[0].destination_name
    member.write_bytes(b"partial")

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(
            environment.executor.recover(
                plan_hash=environment.plan.plan_hash,
                approval_id=environment.approval.approval_id,
            )
        )

    assert raised.value.code is ExecutorErrorCode.RECOVERY_REQUIRED
    assert member.read_bytes() == b"partial"


def test_member_symlink_is_never_followed_or_overwritten(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    transaction = environment.transaction
    _claim_for_recovery(environment)
    environment.journals.record(transaction, "staging_create_started")
    staging = environment.source_folder / transaction.staging_name
    staging.mkdir(mode=0o700)
    metadata = os.stat(staging, follow_symlinks=False)
    environment.journals.record_staging(
        transaction,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )
    outside = tmp_path / "outside.ass"
    outside.write_bytes(b"keep")
    member = staging / environment.plan.members[0].destination_name
    member.symlink_to(outside)

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(
            environment.executor.recover(
                plan_hash=environment.plan.plan_hash,
                approval_id=environment.approval.approval_id,
            )
        )

    assert raised.value.code is ExecutorErrorCode.RECOVERY_REQUIRED
    assert outside.read_bytes() == b"keep"
    assert member.is_symlink()


def test_casefold_equivalent_staging_name_is_a_collision(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    transaction = environment.transaction
    _claim_for_recovery(environment)
    environment.journals.record(transaction, "staging_create_started")
    staging = environment.source_folder / transaction.staging_name
    staging.mkdir(mode=0o700)
    metadata = os.stat(staging, follow_symlinks=False)
    environment.journals.record_staging(
        transaction,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )
    expected = environment.plan.members[0].destination_name
    alternate = expected.swapcase()
    assert alternate != expected
    (staging / alternate).write_bytes(_SUBTITLE_CONTENT)

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(
            environment.executor.recover(
                plan_hash=environment.plan.plan_hash,
                approval_id=environment.approval.approval_id,
            )
        )

    assert raised.value.code is ExecutorErrorCode.DESTINATION_COLLISION


def test_recovery_reconciles_parent_fsync_failure_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    root_identity = os.stat(
        environment.source_folder,
        follow_symlinks=False,
    )
    real_fsync = os.fsync
    failed = False

    def fail_first_root_fsync(file_descriptor: int) -> None:
        nonlocal failed
        metadata = os.fstat(file_descriptor)
        if (
            not failed
            and (metadata.st_dev, metadata.st_ino)
            == (root_identity.st_dev, root_identity.st_ino)
        ):
            failed = True
            raise OSError(errno.EIO, "injected parent fsync failure")
        real_fsync(file_descriptor)

    monkeypatch.setattr(
        executor_module,
        "_native_rename_noreplace",
        _simulated_native_rename,
    )
    monkeypatch.setattr(executor_module.os, "fsync", fail_first_root_fsync)

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(
            environment.executor.apply(
                plan_hash=environment.plan.plan_hash,
                approval_id=environment.approval.approval_id,
            )
        )

    assert raised.value.code is ExecutorErrorCode.RECOVERY_REQUIRED
    assert failed
    assert (environment.source_folder / environment.transaction.destination_name).is_dir()
    monkeypatch.setattr(executor_module.os, "fsync", real_fsync)

    result = asyncio.run(
        environment.executor.recover(
            plan_hash=environment.plan.plan_hash,
            approval_id=environment.approval.approval_id,
        )
    )

    assert result.status == "completed"
    assert environment.fetcher.calls == 1
