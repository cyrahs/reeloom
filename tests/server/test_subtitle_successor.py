from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from reeloom.executor.subtitle_acquisition import SubtitleAcquisitionResult
from reeloom.executor.subtitle_transaction import (
    SubtitleAcquisitionTransactionRecord,
)
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.rename_plan import RootBinding
from reeloom.kernel.subtitle_acquisition import (
    InspectedSubtitleMember,
    SubtitleAcquisitionPlan,
    SubtitleArchiveFormat,
    SubtitleArchiveSetId,
    SubtitleArchiveSource,
    SubtitleArchiveVolume,
    SubtitleReleaseId,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.scheduler import JobStatus
from reeloom.server.subtitle_successor import (
    InMemorySubtitleSuccessorOutbox,
    SubtitleAcquisitionSettlement,
    SubtitleSuccessorError,
    SubtitleSuccessorErrorCode,
    SubtitleSuccessorOutboxState,
    SubtitleFreshScanError,
    SubtitleFreshScan,
    SubtitleSuccessorWorker,
)
from reeloom.server.watcher import FolderSnapshot, NoFollowWatcher

_NOW = datetime(2026, 8, 4, tzinfo=UTC)
_ARCHIVE = b"PK\x03\x04archive"
_SUBTITLE = b"subtitle"


def _case(
    tmp_path: Path,
) -> tuple[
    SubtitleAcquisitionPlan,
    SubtitleAcquisitionResult,
    SubtitleAcquisitionSettlement,
    FolderSnapshot,
]:
    media = tmp_path / "media"
    source = media / "Work"
    source.mkdir(parents=True)
    (source / "episode.mkv").write_bytes(b"video")
    watcher = NoFollowWatcher()
    root = AuthorizedRoot.create(media)
    original = watcher.scan_folder(
        root,
        PurePosixPath("Work"),
        logical_name="Work",
    )
    archive_id = SubtitleArchiveSetId(1)
    archive_source = SubtitleArchiveSource(
        SubtitleReleaseId(1),
        archive_id,
        SubtitleArchiveFormat.ZIP,
        (1,),
        10081,
        95257,
        "b" * 64,
        (
            SubtitleArchiveVolume(
                1,
                34768,
                len(_ARCHIVE),
                hashlib.sha256(_ARCHIVE).hexdigest(),
            ),
        ),
    )
    inspected = InspectedSubtitleMember(
        archive_id,
        PurePosixPath("Subs/E01.ass"),
        len(_SUBTITLE),
        hashlib.sha256(_SUBTITLE).hexdigest(),
    )
    plan = SubtitleAcquisitionPlan.create(
        run_id="run-m13-successor",
        config_revision_id="config-1",
        created_at=_NOW,
        source_root=RootBinding(
            PurePosixPath(media.as_posix()),
            root.device,
            root.inode,
        ),
        source_folder=source.name,
        source_folder_device=original.device,
        source_folder_inode=original.inode,
        folder_generation_id="generation-origin",
        candidate_snapshot_id=original.candidates.snapshot_id,
        tmdb_id=123,
        archives=(archive_source,),
        inspected_members=(inspected,),
    )
    approval = ApprovalRecord.create(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        scope=ApprovalScope.SUBTITLE_ACQUIRE,
        expires_at=_NOW + timedelta(minutes=15),
        nonce="n" * 32,
    )
    transaction = SubtitleAcquisitionTransactionRecord.create(
        plan,
        approval_id=approval.approval_id,
    )
    destination = source / transaction.destination_name
    destination.mkdir()
    (destination / plan.members[0].destination_name).write_bytes(_SUBTITLE)
    destination_stat = destination.stat()
    result = SubtitleAcquisitionResult(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        approval_id=approval.approval_id,
        transaction_id=transaction.transaction_id,
        destination_name=transaction.destination_name,
        destination_device=destination_stat.st_dev,
        destination_inode=destination_stat.st_ino,
        published_count=1,
    )
    settlement = SubtitleAcquisitionSettlement.create(
        plan=plan,
        result=result,
        origin_discovery_id="discovery-origin",
    )
    fresh = watcher.scan_folder(
        root,
        PurePosixPath("Work"),
        logical_name="Work",
    )
    return plan, result, settlement, fresh


def _repository(
    settlement: SubtitleAcquisitionSettlement,
) -> InMemorySubtitleSuccessorOutbox:
    repository = InMemorySubtitleSuccessorOutbox()
    repository.register_origin(
        run_id=settlement.origin_run_id,
        discovery_id=settlement.origin_discovery_id,
        watch_id="watch-anime",
        config_revision=1,
        source_folder=settlement.source_folder,
        snapshot_id=settlement.original_snapshot_id,
    )
    return repository


def _stabilized_claim(
    repository: InMemorySubtitleSuccessorOutbox,
    claim,
    fresh: FolderSnapshot,
):
    assert not repository.stabilize(
        claim,
        snapshot=fresh,
        now=_NOW,
        delay=timedelta(seconds=1),
    )
    stable_claim = repository.claim(
        worker_id=claim.worker_id,
        now=_NOW + timedelta(seconds=1),
        lease_for=timedelta(seconds=30),
    )
    assert stable_claim is not None
    assert repository.stabilize(
        stable_claim,
        snapshot=fresh,
        now=_NOW + timedelta(seconds=1),
        delay=timedelta(seconds=1),
    )
    return stable_claim


def test_settlement_immediately_supersedes_origin_and_registers_fresh_run(
    tmp_path: Path,
) -> None:
    _, _, settlement, fresh = _case(tmp_path)
    repository = _repository(settlement)

    settled = repository.settle(settlement)

    assert settled.created
    assert repository.origin_status(settlement.origin_run_id) == (
        "superseded",
        JobStatus.COMPLETED,
    )
    assert not repository.lineage_allows_automatic_acquisition(
        settlement.origin_run_id
    )
    assert (
        repository.outbox_state(settled.lineage_key)
        is SubtitleSuccessorOutboxState.QUEUED
    )
    claim = repository.claim(
        worker_id="worker-1",
        now=_NOW,
        lease_for=timedelta(seconds=30),
    )
    assert claim is not None
    claim = _stabilized_claim(repository, claim, fresh)

    successor = repository.complete(
        claim,
        snapshot=fresh,
        now=_NOW + timedelta(seconds=1),
    )

    assert successor.predecessor_run_id == settlement.origin_run_id
    assert successor.discovery.snapshot_id != settlement.original_snapshot_id
    assert successor.discovery.source_folder == settlement.source_folder
    assert successor.registration.discovery_id == successor.discovery.discovery_id
    assert (
        repository.outbox_state(settled.lineage_key)
        is SubtitleSuccessorOutboxState.COMPLETED
    )
    assert not repository.lineage_allows_automatic_acquisition(
        successor.registration.run_id
    )


def test_exact_settlement_is_idempotent_under_concurrency(tmp_path: Path) -> None:
    _, _, settlement, _ = _case(tmp_path)
    repository = _repository(settlement)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(repository.settle, (settlement,) * 8))

    assert sum(item.created for item in results) == 1
    assert len({item.lineage_key for item in results}) == 1


def test_only_one_worker_can_claim_and_expired_lease_is_recovered(
    tmp_path: Path,
) -> None:
    _, _, settlement, _ = _case(tmp_path)
    repository = _repository(settlement)
    repository.settle(settlement)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(
            executor.map(
                lambda worker: repository.claim(
                    worker_id=worker,
                    now=_NOW,
                    lease_for=timedelta(seconds=30),
                ),
                ("worker-a", "worker-b"),
            )
        )

    winners = tuple(item for item in claims if item is not None)
    assert len(winners) == 1
    reclaimed = repository.claim(
        worker_id="worker-c",
        now=_NOW + timedelta(seconds=31),
        lease_for=timedelta(seconds=30),
    )
    assert reclaimed is not None
    assert reclaimed.attempt_count == 2


def test_stale_or_incomplete_scan_cannot_create_successor(tmp_path: Path) -> None:
    _, _, settlement, fresh = _case(tmp_path)
    repository = _repository(settlement)
    settled = repository.settle(settlement)
    claim = repository.claim(
        worker_id="worker-1",
        now=_NOW,
        lease_for=timedelta(seconds=30),
    )
    assert claim is not None
    stale = replace(
        fresh,
        candidates=replace(
            fresh.candidates,
            snapshot_id=settlement.original_snapshot_id,
        ),
    )

    with pytest.raises(SubtitleSuccessorError) as raised:
        repository.complete(claim, snapshot=stale, now=_NOW)

    assert raised.value.code is SubtitleSuccessorErrorCode.FRESH_SCAN_REQUIRED
    assert (
        repository.outbox_state(claim.lineage_key)
        is SubtitleSuccessorOutboxState.LEASED
    )


def test_successor_lineage_cannot_automatically_acquire_again(
    tmp_path: Path,
) -> None:
    _, _, settlement, fresh = _case(tmp_path)
    repository = _repository(settlement)
    repository.settle(settlement)
    claim = repository.claim(
        worker_id="worker-1",
        now=_NOW,
        lease_for=timedelta(seconds=30),
    )
    assert claim is not None
    claim = _stabilized_claim(repository, claim, fresh)
    successor = repository.complete(
        claim,
        snapshot=fresh,
        now=_NOW + timedelta(seconds=1),
    )
    second = replace(
        settlement,
        origin_run_id=successor.registration.run_id,
        origin_discovery_id=successor.discovery.discovery_id,
        plan_hash="sha256:" + "f" * 64,
        approval_id="approval-second",
        transaction_id="transaction-second",
        original_snapshot_id=successor.discovery.snapshot_id,
        destination_name="reeloom-acquired-" + "f" * 64,
        destination_inode=settlement.destination_inode + 1,
    )

    with pytest.raises(SubtitleSuccessorError) as raised:
        repository.settle(second)

    assert (
        raised.value.code
        is SubtitleSuccessorErrorCode.LINEAGE_ALREADY_ACQUIRED
    )


def test_settlement_factory_rejects_result_not_bound_to_plan(
    tmp_path: Path,
) -> None:
    plan, result, _, _ = _case(tmp_path)

    with pytest.raises(SubtitleSuccessorError) as raised:
        SubtitleAcquisitionSettlement.create(
            plan=plan,
            result=replace(result, transaction_id="transaction-tampered"),
            origin_discovery_id="discovery-origin",
        )

    assert raised.value.code is SubtitleSuccessorErrorCode.INVALID_REQUEST


def test_worker_drives_fresh_scan_from_outbox_capability(tmp_path: Path) -> None:
    _, _, settlement, fresh = _case(tmp_path)
    repository = _repository(settlement)
    settled = repository.settle(settlement)
    observed: list[tuple[str, str]] = []

    class Scanner:
        def scan(self, claim):
            observed.append((claim.watch_id, claim.settlement.source_folder))
            return SubtitleFreshScan(fresh, timedelta(seconds=5))

    worker = SubtitleSuccessorWorker(repository, Scanner())

    assert worker.process_one(worker_id="worker-1", now=_NOW) is None
    assert (
        repository.outbox_state(settled.lineage_key)
        is SubtitleSuccessorOutboxState.RETRY_WAIT
    )
    assert (
        worker.process_one(
            worker_id="worker-1",
            now=_NOW + timedelta(seconds=4),
        )
        is None
    )
    successor = worker.process_one(
        worker_id="worker-1",
        now=_NOW + timedelta(seconds=5),
    )

    assert successor is not None
    assert observed == [
        ("watch-anime", "Work"),
        ("watch-anime", "Work"),
    ]


def test_worker_restarts_stability_window_when_snapshot_changes(
    tmp_path: Path,
) -> None:
    _, _, settlement, fresh = _case(tmp_path)
    repository = _repository(settlement)
    settled = repository.settle(settlement)
    changed = replace(
        fresh,
        inventory_id="inventory-changed",
        candidates=replace(
            fresh.candidates,
            snapshot_id="snapshot-changed",
        ),
    )
    scans = iter(
        (
            SubtitleFreshScan(fresh, timedelta(seconds=5)),
            SubtitleFreshScan(changed, timedelta(seconds=5)),
            SubtitleFreshScan(changed, timedelta(seconds=5)),
        )
    )

    class Scanner:
        def scan(self, claim):
            del claim
            return next(scans)

    worker = SubtitleSuccessorWorker(repository, Scanner())

    assert worker.process_one(worker_id="worker-1", now=_NOW) is None
    assert (
        worker.process_one(
            worker_id="worker-1",
            now=_NOW + timedelta(seconds=5),
        )
        is None
    )
    assert repository.outbox_state(settled.lineage_key) is (
        SubtitleSuccessorOutboxState.RETRY_WAIT
    )
    successor = worker.process_one(
        worker_id="worker-1",
        now=_NOW + timedelta(seconds=10),
    )

    assert successor is not None
    assert successor.discovery.snapshot_id == "snapshot-changed"


@pytest.mark.parametrize(
    ("retryable", "expected_state"),
    (
        (True, SubtitleSuccessorOutboxState.RETRY_WAIT),
        (False, SubtitleSuccessorOutboxState.BLOCKED),
    ),
)
def test_worker_distinguishes_retryable_scan_failure_from_attention(
    tmp_path: Path,
    retryable: bool,
    expected_state: SubtitleSuccessorOutboxState,
) -> None:
    _, _, settlement, _ = _case(tmp_path)
    repository = _repository(settlement)
    settled = repository.settle(settlement)

    class Scanner:
        def scan(self, claim):
            raise SubtitleFreshScanError(retryable=retryable)

    worker = SubtitleSuccessorWorker(repository, Scanner())

    assert worker.process_one(worker_id="worker-1", now=_NOW) is None
    assert repository.outbox_state(settled.lineage_key) is expected_state


def test_completed_successor_registration_is_idempotent(tmp_path: Path) -> None:
    _, _, settlement, fresh = _case(tmp_path)
    repository = _repository(settlement)
    repository.settle(settlement)
    claim = repository.claim(
        worker_id="worker-1",
        now=_NOW,
        lease_for=timedelta(seconds=30),
    )
    assert claim is not None
    claim = _stabilized_claim(repository, claim, fresh)

    first = repository.complete(
        claim,
        snapshot=fresh,
        now=_NOW + timedelta(seconds=1),
    )
    second = repository.complete(
        claim,
        snapshot=fresh,
        now=_NOW + timedelta(seconds=1),
    )

    assert second == first
