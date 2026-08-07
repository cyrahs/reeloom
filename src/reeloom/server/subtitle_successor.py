from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from reeloom.executor.subtitle_acquisition import SubtitleAcquisitionResult
from reeloom.executor.subtitle_transaction import (
    SubtitleAcquisitionTransactionRecord,
)
from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.naming import filesystem_name_key
from reeloom.kernel.subtitle_acquisition import SubtitleAcquisitionPlan
from reeloom.server.config import ServerWorkType
from reeloom.server.scheduler import Discovery, JobStatus, RunRegistration, _id
from reeloom.server.watcher import FolderEntryKind, FolderSnapshot

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PLAN_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_LINEAGE = re.compile(r"^subtitle-lineage-v1-[0-9a-f]{64}$")
_MAX_LEASE_SECONDS = 3600


class SubtitleSuccessorErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    ORIGIN_NOT_FOUND = "origin_not_found"
    ORIGIN_STATE_CONFLICT = "origin_state_conflict"
    LINEAGE_ALREADY_ACQUIRED = "lineage_already_acquired"
    OUTBOX_EMPTY = "outbox_empty"
    LEASE_CONFLICT = "lease_conflict"
    LEASE_EXPIRED = "lease_expired"
    FRESH_SCAN_REQUIRED = "fresh_scan_required"
    SUCCESSOR_CONFLICT = "successor_conflict"


class SubtitleSuccessorError(RuntimeError):
    def __init__(self, code: SubtitleSuccessorErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class SubtitleSuccessorOutboxState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SubtitleSuccessorMember:
    destination_name: str
    size_bytes: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.destination_name, str)
            or not self.destination_name
            or len(self.destination_name.encode("utf-8")) > 255
            or "/" in self.destination_name
            or "\\" in self.destination_name
            or type(self.size_bytes) is not int
            or self.size_bytes < 1
        ):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionSettlement:
    origin_run_id: str
    origin_discovery_id: str
    plan_hash: str
    approval_id: str
    transaction_id: str
    source_folder: str
    source_folder_device: int
    source_folder_inode: int
    original_snapshot_id: str
    destination_name: str
    destination_device: int
    destination_inode: int
    members: tuple[SubtitleSuccessorMember, ...]

    @classmethod
    def create(
        cls,
        *,
        plan: SubtitleAcquisitionPlan,
        result: SubtitleAcquisitionResult,
        origin_discovery_id: str,
    ) -> SubtitleAcquisitionSettlement:
        if (
            not isinstance(plan, SubtitleAcquisitionPlan)
            or not plan.verify_hash()
            or not isinstance(result, SubtitleAcquisitionResult)
            or result.status != "completed"
        ):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )
        transaction = SubtitleAcquisitionTransactionRecord.create(
            plan,
            approval_id=result.approval_id,
        )
        if (
            result.run_id != plan.run_id
            or result.plan_hash != plan.plan_hash
            or result.transaction_id != transaction.transaction_id
            or result.destination_name != transaction.destination_name
            or result.published_count != len(plan.members)
        ):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )
        return cls(
            origin_run_id=plan.run_id,
            origin_discovery_id=origin_discovery_id,
            plan_hash=plan.plan_hash,
            approval_id=result.approval_id,
            transaction_id=result.transaction_id,
            source_folder=plan.source_folder,
            source_folder_device=plan.source_folder_device,
            source_folder_inode=plan.source_folder_inode,
            original_snapshot_id=plan.candidate_snapshot_id,
            destination_name=result.destination_name,
            destination_device=result.destination_device,
            destination_inode=result.destination_inode,
            members=tuple(
                SubtitleSuccessorMember(
                    item.destination_name,
                    item.size_bytes,
                )
                for item in plan.members
            ),
        )

    def __post_init__(self) -> None:
        identifiers = (
            self.origin_run_id,
            self.origin_discovery_id,
            self.approval_id,
            self.transaction_id,
        )
        if (
            any(
                not isinstance(item, str) or _ID.fullmatch(item) is None
                for item in identifiers
            )
            or not isinstance(self.plan_hash, str)
            or _PLAN_HASH.fullmatch(self.plan_hash) is None
            or not isinstance(self.source_folder, str)
            or not self.source_folder
            or "/" in self.source_folder
            or "\\" in self.source_folder
            or not isinstance(self.original_snapshot_id, str)
            or not self.original_snapshot_id
            or not isinstance(self.destination_name, str)
            or not self.destination_name.startswith("reeloom-acquired-")
            or "/" in self.destination_name
            or "\\" in self.destination_name
            or any(
                type(item) is not int or item < 0
                for item in (
                    self.source_folder_device,
                    self.source_folder_inode,
                    self.destination_device,
                    self.destination_inode,
                )
            )
            or not isinstance(self.members, tuple)
            or not 1 <= len(self.members) <= 256
            or any(
                not isinstance(item, SubtitleSuccessorMember)
                for item in self.members
            )
            or len(
                {
                    filesystem_name_key(item.destination_name)
                    for item in self.members
                }
            )
            != len(self.members)
        ):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )

    @property
    def member_manifest_json(self) -> str:
        return json.dumps(
            [
                {
                    "destination_name": item.destination_name,
                    "size_bytes": item.size_bytes,
                }
                for item in self.members
            ],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class SubtitleSuccessorClaim:
    lineage_key: str
    settlement: SubtitleAcquisitionSettlement
    watch_id: str
    config_revision: int
    worker_id: str
    attempt_count: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class SubtitleSuccessorRegistration:
    lineage_key: str
    predecessor_run_id: str
    discovery: Discovery
    registration: RunRegistration


@dataclass(frozen=True, slots=True)
class SubtitleFreshScan:
    snapshot: FolderSnapshot
    settle_for: timedelta

    def __post_init__(self) -> None:
        if (
            not isinstance(self.snapshot, FolderSnapshot)
            or not isinstance(self.settle_for, timedelta)
            or not timedelta(seconds=1)
            <= self.settle_for
            <= timedelta(days=7)
        ):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )


@dataclass(frozen=True, slots=True)
class SubtitleSettlementResult:
    lineage_key: str
    created: bool


@dataclass(slots=True)
class _Origin:
    run_id: str
    discovery_id: str
    watch_id: str
    config_revision: int
    source_folder: str
    snapshot_id: str
    status: str
    job_status: JobStatus
    lineage_key: str | None = None


@dataclass(slots=True)
class _Outbox:
    settlement: SubtitleAcquisitionSettlement
    watch_id: str
    config_revision: int
    state: SubtitleSuccessorOutboxState
    attempt_count: int = 0
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    available_at: datetime | None = None
    registration: SubtitleSuccessorRegistration | None = None
    stabilizing_inventory_id: str | None = None
    stabilizing_snapshot_id: str | None = None


def subtitle_lineage_key(origin_discovery_id: str) -> str:
    if not isinstance(origin_discovery_id, str) or _ID.fullmatch(
        origin_discovery_id
    ) is None:
        raise SubtitleSuccessorError(
            SubtitleSuccessorErrorCode.INVALID_REQUEST
        )
    return "subtitle-lineage-v1-" + hashlib.sha256(
        origin_discovery_id.encode("utf-8")
    ).hexdigest()


class InMemorySubtitleSuccessorOutbox:
    """Transactional fake for settlement, lineage gate and successor outbox."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._origins: dict[str, _Origin] = {}
        self._settlements: dict[str, SubtitleAcquisitionSettlement] = {}
        self._outbox: dict[str, _Outbox] = {}
        self._successors: dict[str, SubtitleSuccessorRegistration] = {}

    def register_origin(
        self,
        *,
        run_id: str,
        discovery_id: str,
        watch_id: str,
        config_revision: int,
        source_folder: str,
        snapshot_id: str,
        status: str = "running",
        lineage_key: str | None = None,
    ) -> None:
        if (
            any(
                _ID.fullmatch(item) is None
                for item in (run_id, discovery_id, watch_id)
            )
            or type(config_revision) is not int
            or config_revision < 1
            or not source_folder
            or not snapshot_id
            or status not in {"running", "awaiting_approval", "applying"}
            or (
                lineage_key is not None
                and _LINEAGE.fullmatch(lineage_key) is None
            )
        ):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )
        with self._lock:
            if run_id in self._origins:
                raise SubtitleSuccessorError(
                    SubtitleSuccessorErrorCode.SUCCESSOR_CONFLICT
                )
            self._origins[run_id] = _Origin(
                run_id,
                discovery_id,
                watch_id,
                config_revision,
                source_folder,
                snapshot_id,
                status,
                JobStatus.RUNNING,
                lineage_key,
            )

    def settle(
        self,
        settlement: SubtitleAcquisitionSettlement,
    ) -> SubtitleSettlementResult:
        if not isinstance(settlement, SubtitleAcquisitionSettlement):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )
        with self._lock:
            origin = self._origins.get(settlement.origin_run_id)
            if origin is None:
                raise SubtitleSuccessorError(
                    SubtitleSuccessorErrorCode.ORIGIN_NOT_FOUND
                )
            if (
                origin.discovery_id != settlement.origin_discovery_id
                or origin.source_folder != settlement.source_folder
                or origin.snapshot_id != settlement.original_snapshot_id
            ):
                raise SubtitleSuccessorError(
                    SubtitleSuccessorErrorCode.INVALID_REQUEST
                )
            lineage_key = origin.lineage_key or subtitle_lineage_key(
                origin.discovery_id
            )
            existing = self._settlements.get(lineage_key)
            if existing is not None:
                if existing == settlement:
                    return SubtitleSettlementResult(lineage_key, False)
                raise SubtitleSuccessorError(
                    SubtitleSuccessorErrorCode.LINEAGE_ALREADY_ACQUIRED
                )
            if origin.lineage_key is not None or origin.status not in {
                "running",
                "awaiting_approval",
                "applying",
            }:
                raise SubtitleSuccessorError(
                    SubtitleSuccessorErrorCode.LINEAGE_ALREADY_ACQUIRED
                    if origin.lineage_key is not None
                    else SubtitleSuccessorErrorCode.ORIGIN_STATE_CONFLICT
                )
            origin.lineage_key = lineage_key
            origin.status = "superseded"
            origin.job_status = JobStatus.COMPLETED
            self._settlements[lineage_key] = settlement
            self._outbox[lineage_key] = _Outbox(
                settlement,
                origin.watch_id,
                origin.config_revision,
                SubtitleSuccessorOutboxState.QUEUED,
            )
            return SubtitleSettlementResult(lineage_key, True)

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> SubtitleSuccessorClaim | None:
        _validate_lease(worker_id, now, lease_for)
        with self._lock:
            for item in self._outbox.values():
                if (
                    item.state is SubtitleSuccessorOutboxState.LEASED
                    and item.lease_expires_at is not None
                    and item.lease_expires_at <= now
                ):
                    item.state = SubtitleSuccessorOutboxState.RETRY_WAIT
                    item.worker_id = None
                    item.lease_expires_at = None
                    item.available_at = now
            eligible = sorted(
                (
                    (lineage, item)
                    for lineage, item in self._outbox.items()
                    if item.state
                    in {
                        SubtitleSuccessorOutboxState.QUEUED,
                        SubtitleSuccessorOutboxState.RETRY_WAIT,
                    }
                    and (item.available_at is None or item.available_at <= now)
                ),
                key=lambda item: item[0],
            )
            if not eligible:
                return None
            lineage_key, item = eligible[0]
            item.state = SubtitleSuccessorOutboxState.LEASED
            item.worker_id = worker_id
            item.attempt_count += 1
            item.lease_expires_at = now + lease_for
            return SubtitleSuccessorClaim(
                lineage_key,
                item.settlement,
                item.watch_id,
                item.config_revision,
                worker_id,
                item.attempt_count,
                item.lease_expires_at,
            )

    def retry(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        now: datetime,
        delay: timedelta,
    ) -> None:
        if delay < timedelta(0) or delay > timedelta(hours=24):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )
        with self._lock:
            item = self._require_lease(claim, now=now)
            if claim.attempt_count >= 100:
                item.state = SubtitleSuccessorOutboxState.BLOCKED
                item.worker_id = None
                item.lease_expires_at = None
                return
            item.state = SubtitleSuccessorOutboxState.RETRY_WAIT
            item.worker_id = None
            item.lease_expires_at = None
            item.available_at = now + delay

    def block(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        now: datetime,
    ) -> None:
        with self._lock:
            item = self._require_lease(claim, now=now)
            item.state = SubtitleSuccessorOutboxState.BLOCKED
            item.worker_id = None
            item.lease_expires_at = None

    def stabilize(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        snapshot: FolderSnapshot,
        now: datetime,
        delay: timedelta,
    ) -> bool:
        if not timedelta(seconds=1) <= delay <= timedelta(days=7):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )
        with self._lock:
            item = self._require_lease(claim, now=now)
            _validate_fresh_snapshot(item.settlement, snapshot)
            fingerprint = (
                snapshot.inventory_id,
                snapshot.candidates.snapshot_id,
            )
            if fingerprint == (
                item.stabilizing_inventory_id,
                item.stabilizing_snapshot_id,
            ):
                return True
            item.stabilizing_inventory_id = fingerprint[0]
            item.stabilizing_snapshot_id = fingerprint[1]
            item.state = SubtitleSuccessorOutboxState.RETRY_WAIT
            item.worker_id = None
            item.lease_expires_at = None
            item.available_at = now + delay
            return False

    def complete(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        snapshot: FolderSnapshot,
        now: datetime,
    ) -> SubtitleSuccessorRegistration:
        with self._lock:
            completed = self._outbox.get(claim.lineage_key)
            if (
                completed is not None
                and completed.state
                is SubtitleSuccessorOutboxState.COMPLETED
                and completed.registration is not None
                and completed.settlement == claim.settlement
            ):
                _validate_fresh_snapshot(completed.settlement, snapshot)
                return completed.registration
            item = self._require_lease(claim, now=now)
            _validate_fresh_snapshot(item.settlement, snapshot)
            if (
                item.stabilizing_inventory_id,
                item.stabilizing_snapshot_id,
            ) != (
                snapshot.inventory_id,
                snapshot.candidates.snapshot_id,
            ):
                raise SubtitleSuccessorError(
                    SubtitleSuccessorErrorCode.FRESH_SCAN_REQUIRED
                )
            discovery_id = _id(
                "discovery",
                claim.lineage_key,
                snapshot.inventory_id,
                snapshot.candidates.snapshot_id,
            )
            run_id = _id("run", discovery_id)
            registration = SubtitleSuccessorRegistration(
                lineage_key=claim.lineage_key,
                predecessor_run_id=item.settlement.origin_run_id,
                discovery=Discovery(
                    discovery_id=discovery_id,
                    watch_id=item.watch_id,
                    config_revision=item.config_revision,
                    snapshot_id=snapshot.candidates.snapshot_id,
                    work_type=ServerWorkType.ANIME,
                    discovered_at=now,
                    snapshot=snapshot.candidates,
                    source_folder=snapshot.name,
                    folder_generation_id=_id(
                        "generation",
                        claim.lineage_key,
                        snapshot.inventory_id,
                    ),
                    inventory_id=snapshot.inventory_id,
                    source_folder_device=snapshot.device,
                    source_folder_inode=snapshot.inode,
                ),
                registration=RunRegistration(
                    run_id=run_id,
                    job_id=_id("job", run_id),
                    discovery_id=discovery_id,
                    config_revision=item.config_revision,
                    work_type=ServerWorkType.ANIME,
                    source_capability=_id("capability", run_id),
                ),
            )
            item.state = SubtitleSuccessorOutboxState.COMPLETED
            item.worker_id = None
            item.lease_expires_at = None
            item.registration = registration
            self._successors[run_id] = registration
            self._origins[run_id] = _Origin(
                run_id,
                discovery_id,
                item.watch_id,
                item.config_revision,
                snapshot.name,
                snapshot.candidates.snapshot_id,
                "registered",
                JobStatus.PENDING,
                claim.lineage_key,
            )
            return registration

    def lineage_allows_automatic_acquisition(self, run_id: str) -> bool:
        with self._lock:
            origin = self._origins.get(run_id)
            if origin is None:
                raise SubtitleSuccessorError(
                    SubtitleSuccessorErrorCode.ORIGIN_NOT_FOUND
                )
            return (
                origin.lineage_key is None
                or origin.lineage_key not in self._settlements
            )

    def origin_status(self, run_id: str) -> tuple[str, JobStatus]:
        with self._lock:
            origin = self._origins.get(run_id)
            if origin is None:
                raise SubtitleSuccessorError(
                    SubtitleSuccessorErrorCode.ORIGIN_NOT_FOUND
                )
            return origin.status, origin.job_status

    def outbox_state(self, lineage_key: str) -> SubtitleSuccessorOutboxState:
        with self._lock:
            return self._outbox[lineage_key].state

    def _require_lease(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        now: datetime,
    ) -> _Outbox:
        if not isinstance(claim, SubtitleSuccessorClaim):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )
        item = self._outbox.get(claim.lineage_key)
        if (
            item is None
            or item.state is not SubtitleSuccessorOutboxState.LEASED
            or item.worker_id != claim.worker_id
            or item.attempt_count != claim.attempt_count
            or item.lease_expires_at != claim.lease_expires_at
        ):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.LEASE_CONFLICT
            )
        if now >= claim.lease_expires_at:
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.LEASE_EXPIRED
            )
        return item


def _validate_lease(
    worker_id: str,
    now: datetime,
    lease_for: timedelta,
) -> None:
    if (
        not isinstance(worker_id, str)
        or _ID.fullmatch(worker_id) is None
        or not isinstance(now, datetime)
        or now.tzinfo is None
        or not isinstance(lease_for, timedelta)
        or not timedelta(seconds=1)
        <= lease_for
        <= timedelta(seconds=_MAX_LEASE_SECONDS)
    ):
        raise SubtitleSuccessorError(
            SubtitleSuccessorErrorCode.INVALID_REQUEST
        )


def _validate_fresh_snapshot(
    settlement: SubtitleAcquisitionSettlement,
    snapshot: FolderSnapshot,
) -> None:
    if (
        not isinstance(snapshot, FolderSnapshot)
        or snapshot.name != settlement.source_folder
        or (snapshot.device, snapshot.inode)
        != (
            settlement.source_folder_device,
            settlement.source_folder_inode,
        )
        or snapshot.candidates.snapshot_id
        == settlement.original_snapshot_id
        or any(
            item.relative_path.name.startswith(".reeloom-acquiring-")
            for item in snapshot.entries
        )
    ):
        raise SubtitleSuccessorError(
            SubtitleSuccessorErrorCode.FRESH_SCAN_REQUIRED
        )
    destination_entries = tuple(
        item
        for item in snapshot.entries
        if item.relative_path.as_posix() == settlement.destination_name
    )
    if (
        len(destination_entries) != 1
        or destination_entries[0].kind is not FolderEntryKind.DIRECTORY
        or (
            destination_entries[0].device,
            destination_entries[0].inode,
        )
        != (settlement.destination_device, settlement.destination_inode)
    ):
        raise SubtitleSuccessorError(
            SubtitleSuccessorErrorCode.FRESH_SCAN_REQUIRED
        )
    candidates = {
        item.relative_path.as_posix(): item
        for item in snapshot.candidates.files
    }
    expected = {
        (
            f"{settlement.source_folder}/{settlement.destination_name}/"
            f"{member.destination_name}"
        ): member.size_bytes
        for member in settlement.members
    }
    actual_subtitles = {
        path
        for path, item in candidates.items()
        if item.kind is CandidateKind.SUBTITLE
    }
    if (
        actual_subtitles != set(expected)
        or
        any(
            path not in candidates
            or candidates[path].kind is not CandidateKind.SUBTITLE
            or candidates[path].size_bytes != size
            for path, size in expected.items()
        )
        or not any(
            item.kind is CandidateKind.VIDEO
            for item in snapshot.candidates.files
        )
    ):
        raise SubtitleSuccessorError(
            SubtitleSuccessorErrorCode.FRESH_SCAN_REQUIRED
        )


class SubtitleSuccessorOutboxPort(Protocol):
    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> SubtitleSuccessorClaim | None: ...

    def retry(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        now: datetime,
        delay: timedelta,
    ) -> None: ...

    def block(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        now: datetime,
    ) -> None: ...

    def stabilize(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        snapshot: FolderSnapshot,
        now: datetime,
        delay: timedelta,
    ) -> bool: ...

    def complete(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        snapshot: FolderSnapshot,
        now: datetime,
    ) -> SubtitleSuccessorRegistration: ...


class SubtitleFreshScanner(Protocol):
    """Scan one trusted watch/folder capability; it never accepts a path."""

    def scan(self, claim: SubtitleSuccessorClaim) -> SubtitleFreshScan: ...


class SubtitleFreshScanError(RuntimeError):
    def __init__(self, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__("fresh_subtitle_scan_failed")


@dataclass(frozen=True, slots=True)
class SubtitleSuccessorWorker:
    outbox: SubtitleSuccessorOutboxPort
    scanner: SubtitleFreshScanner
    lease_for: timedelta = timedelta(seconds=30)
    retry_delay: timedelta = timedelta(seconds=10)

    def process_one(
        self,
        *,
        worker_id: str,
        now: datetime,
    ) -> SubtitleSuccessorRegistration | None:
        claim = self.outbox.claim(
            worker_id=worker_id,
            now=now,
            lease_for=self.lease_for,
        )
        if claim is None:
            return None
        try:
            scan = self.scanner.scan(claim)
            if not isinstance(scan, SubtitleFreshScan):
                raise SubtitleSuccessorError(
                    SubtitleSuccessorErrorCode.FRESH_SCAN_REQUIRED
                )
            if not self.outbox.stabilize(
                claim,
                snapshot=scan.snapshot,
                now=now,
                delay=scan.settle_for,
            ):
                return None
            return self.outbox.complete(
                claim,
                snapshot=scan.snapshot,
                now=now,
            )
        except SubtitleFreshScanError as error:
            if not error.retryable:
                self.outbox.block(claim, now=now)
                return None
            self.outbox.retry(
                claim,
                now=now,
                delay=self.retry_delay,
            )
            return None
        except SubtitleSuccessorError as error:
            if error.code is SubtitleSuccessorErrorCode.FRESH_SCAN_REQUIRED:
                self.outbox.block(claim, now=now)
                return None
            raise
        except Exception:
            self.outbox.retry(
                claim,
                now=now,
                delay=self.retry_delay,
            )
            return None
