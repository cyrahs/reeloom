from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from reeloom.kernel.candidates import CandidateKind
from reeloom.server.config import ServerWorkType
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.watcher import (
    FolderScan,
    FolderSnapshot,
    WatchFile,
    WatchSnapshot,
)


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Discovery:
    discovery_id: str
    watch_id: str
    config_revision: int
    snapshot_id: str
    work_type: ServerWorkType
    discovered_at: datetime
    snapshot: WatchSnapshot | None = None
    source_folder: str | None = None
    folder_generation_id: str | None = None
    inventory_id: str | None = None
    source_folder_device: int | None = None
    source_folder_inode: int | None = None


@dataclass(frozen=True, slots=True)
class PollResult:
    mutated: bool
    discovery: Discovery | None = None


@dataclass(frozen=True, slots=True)
class FolderPollResult:
    mutated: bool
    discoveries: tuple[Discovery, ...] = ()
    disposition_run_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunRegistration:
    run_id: str
    job_id: str
    discovery_id: str
    config_revision: int
    work_type: ServerWorkType
    source_capability: str


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: str
    run_id: str
    boot_id: str
    status: JobStatus


@dataclass(frozen=True, slots=True)
class AgentJobContext:
    registration: RunRegistration
    discovery: Discovery


@dataclass(slots=True)
class _WatchState:
    config_revision: int
    fence: int
    work_type: ServerWorkType
    settle_interval_seconds: int
    semantic_v2: bool = False


@dataclass(slots=True)
class _Observation:
    file: WatchFile
    first_observed_at: datetime
    stable_at: datetime | None = None


@dataclass(slots=True)
class _FolderObservation:
    folder: FolderSnapshot
    config_revision: int
    first_observed_at: datetime
    stable_at: datetime | None = None
    discovery_id: str | None = None


@dataclass(slots=True)
class _Job:
    job_id: str
    run_id: str
    status: JobStatus = JobStatus.PENDING
    boot_id: str | None = None


def _id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:32]}"


class InMemorySchedulerRepository:
    """Contract fake mirroring the narrow PostgreSQL scheduler operations."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._watches: dict[str, _WatchState] = {}
        self._observations: dict[str, dict[str, _Observation]] = {}
        self._folder_observations: dict[
            str, dict[str, _FolderObservation]
        ] = {}
        self._discoveries: dict[str, Discovery] = {}
        self._discovery_by_snapshot: dict[tuple[str, int, str], str] = {}
        self._runs: dict[str, RunRegistration] = {}
        self._run_by_discovery: dict[str, str] = {}
        self._jobs: dict[str, _Job] = {}
        self.observation_mutations = 0
        self.audit_count = 0

    @property
    def run_count(self) -> int:
        return len(self._runs)

    @property
    def job_count(self) -> int:
        return len(self._jobs)

    def configure_watch(
        self,
        *,
        watch_id: str,
        config_revision: int,
        fence: int,
        work_type: ServerWorkType,
        settle_interval_seconds: int,
        semantic_v2: bool = False,
    ) -> None:
        with self._lock:
            previous = self._watches.get(watch_id)
            self._watches[watch_id] = _WatchState(
                config_revision=config_revision,
                fence=fence,
                work_type=work_type,
                settle_interval_seconds=settle_interval_seconds,
                semantic_v2=semantic_v2,
            )
            if (
                previous is None
                or previous.config_revision != config_revision
                or previous.fence != fence
                or previous.semantic_v2 != semantic_v2
            ):
                self._observations[watch_id] = {}
                self._folder_observations[watch_id] = {
                    name: item
                    for name, item in self._folder_observations.get(
                        watch_id, {}
                    ).items()
                    if item.discovery_id is not None
                }
            else:
                self._observations.setdefault(watch_id, {})
                self._folder_observations.setdefault(watch_id, {})

    def reconcile_poll(
        self,
        *,
        watch_id: str,
        config_revision: int,
        fence: int,
        observed_at: datetime,
        snapshot: WatchSnapshot,
    ) -> PollResult:
        with self._lock:
            state = self._watches.get(watch_id)
            if state is None:
                raise ServerError(ServerErrorCode.WATCH_NOT_FOUND)
            if (
                state.config_revision != config_revision
                or state.fence != fence
            ):
                raise ServerError(ServerErrorCode.STALE_WATCH_SCAN)
            observations = self._observations[watch_id]
            current = {
                item.relative_path.as_posix(): item
                for item in snapshot.files
            }
            mutated = False
            for removed in set(observations) - set(current):
                del observations[removed]
                mutated = True
            for path, item in current.items():
                previous = observations.get(path)
                previous_identity = (
                    None
                    if previous is None
                    else (
                        previous.file.semantic_identity
                        if state.semantic_v2
                        else previous.file.identity
                    )
                )
                identity = (
                    item.semantic_identity
                    if state.semantic_v2
                    else item.identity
                )
                if previous is None or previous_identity != identity:
                    observations[path] = _Observation(
                        file=item,
                        first_observed_at=observed_at,
                    )
                    mutated = True

            all_settled = bool(observations) and any(
                item.file.kind is CandidateKind.VIDEO
                for item in observations.values()
            )
            threshold = timedelta(
                seconds=state.settle_interval_seconds
            )
            for item in observations.values():
                if item.stable_at is None:
                    if observed_at - item.first_observed_at >= threshold:
                        item.stable_at = observed_at
                        mutated = True
                    else:
                        all_settled = False
            discovery: Discovery | None = None
            snapshot_id = (
                snapshot.semantic_snapshot_id
                if state.semantic_v2
                else snapshot.snapshot_id
            )
            key = (watch_id, config_revision, snapshot_id)
            if all_settled:
                discovery_id = self._discovery_by_snapshot.get(key)
                if discovery_id is None:
                    discovery = Discovery(
                        discovery_id=_id(
                            "discovery",
                            watch_id,
                            str(config_revision),
                            snapshot_id,
                        ),
                        watch_id=watch_id,
                        config_revision=config_revision,
                        snapshot_id=snapshot_id,
                        work_type=state.work_type,
                        discovered_at=observed_at,
                        snapshot=snapshot,
                    )
                    self._discoveries[discovery.discovery_id] = discovery
                    self._discovery_by_snapshot[key] = (
                        discovery.discovery_id
                    )
                    self.audit_count += 1
                    mutated = True
                else:
                    discovery = self._discoveries[discovery_id]
            if mutated:
                self.observation_mutations += 1
            return PollResult(mutated=mutated, discovery=discovery)

    def reconcile_folders(
        self,
        *,
        watch_id: str,
        config_revision: int,
        fence: int,
        observed_at: datetime,
        scan: FolderScan,
    ) -> FolderPollResult:
        with self._lock:
            state = self._watches.get(watch_id)
            if state is None:
                raise ServerError(ServerErrorCode.WATCH_NOT_FOUND)
            if (
                state.config_revision != config_revision
                or state.fence != fence
            ):
                raise ServerError(ServerErrorCode.STALE_WATCH_SCAN)
            observations = self._folder_observations[watch_id]
            current_names = {
                item.name for item in scan.folders
            } | {item.name for item in scan.blocked}
            mutated = False
            for name in tuple(set(observations) - current_names):
                if observations[name].discovery_id is None:
                    del observations[name]
                    mutated = True

            threshold = timedelta(seconds=state.settle_interval_seconds)
            discoveries: list[Discovery] = []
            for folder in scan.folders:
                previous = observations.get(folder.name)
                identity = (
                    (
                        folder.semantic_inventory_id,
                        folder.candidates.semantic_snapshot_id,
                    )
                    if state.semantic_v2
                    else (
                        folder.device,
                        folder.inode,
                        folder.inventory_id,
                        folder.candidates.snapshot_id,
                    )
                )
                previous_identity = (
                    None
                    if previous is None
                    else (
                        (
                            previous.folder.semantic_inventory_id,
                            previous.folder.candidates.semantic_snapshot_id,
                        )
                        if state.semantic_v2
                        else (
                            previous.folder.device,
                            previous.folder.inode,
                            previous.folder.inventory_id,
                            previous.folder.candidates.snapshot_id,
                        )
                    )
                )
                if (
                    previous is None
                    or previous_identity != identity
                    or (
                        previous.discovery_id is None
                        and previous.config_revision != config_revision
                    )
                ):
                    observations[folder.name] = _FolderObservation(
                        folder=folder,
                        config_revision=config_revision,
                        first_observed_at=observed_at,
                    )
                    previous = observations[folder.name]
                    mutated = True
                if previous.discovery_id is not None:
                    continue
                if previous.stable_at is None:
                    if (
                        observed_at - previous.first_observed_at
                        < threshold
                    ):
                        continue
                    previous.stable_at = observed_at
                    mutated = True
                inventory_id = (
                    folder.semantic_inventory_id
                    if state.semantic_v2
                    else folder.inventory_id
                )
                snapshot_id = (
                    folder.candidates.semantic_snapshot_id
                    if state.semantic_v2
                    else folder.candidates.snapshot_id
                )
                generation_parts = (
                    (
                        watch_id,
                        folder.name,
                        inventory_id,
                        previous.first_observed_at.isoformat(),
                    )
                    if state.semantic_v2
                    else (
                        watch_id,
                        folder.name,
                        str(folder.device),
                        str(folder.inode),
                        inventory_id,
                        previous.first_observed_at.isoformat(),
                    )
                )
                generation_id = _id("folder", *generation_parts)
                discovery_id = _id(
                    "discovery",
                    watch_id,
                    str(config_revision),
                    generation_id,
                    snapshot_id,
                )
                discovery = Discovery(
                    discovery_id=discovery_id,
                    watch_id=watch_id,
                    config_revision=config_revision,
                    snapshot_id=snapshot_id,
                    work_type=state.work_type,
                    discovered_at=observed_at,
                    snapshot=folder.candidates,
                    source_folder=folder.name,
                    folder_generation_id=generation_id,
                    inventory_id=inventory_id,
                    source_folder_device=folder.device,
                    source_folder_inode=folder.inode,
                )
                self._discoveries[discovery_id] = discovery
                self._discovery_by_snapshot[
                    (watch_id, config_revision, generation_id)
                ] = discovery_id
                previous.discovery_id = discovery_id
                discoveries.append(discovery)
                self.audit_count += 1
                mutated = True
            if mutated:
                self.observation_mutations += 1
            return FolderPollResult(mutated, tuple(discoveries))

    def register_run(self, *, discovery_id: str) -> RunRegistration:
        with self._lock:
            existing = self._run_by_discovery.get(discovery_id)
            if existing is not None:
                return self._runs[existing]
            discovery = self._discoveries.get(discovery_id)
            if discovery is None:
                raise ServerError(ServerErrorCode.DISCOVERY_NOT_FOUND)
            run_id = _id("run", discovery_id)
            job_id = _id("job", run_id)
            registration = RunRegistration(
                run_id=run_id,
                job_id=job_id,
                discovery_id=discovery_id,
                config_revision=discovery.config_revision,
                work_type=discovery.work_type,
                source_capability=_id("capability", run_id),
            )
            self._runs[run_id] = registration
            self._run_by_discovery[discovery_id] = run_id
            self._jobs[job_id] = _Job(job_id=job_id, run_id=run_id)
            self.audit_count += 1
            return registration

    def claim_job(self, *, boot_id: str) -> ClaimedJob | None:
        with self._lock:
            pending = sorted(
                (
                    item
                    for item in self._jobs.values()
                    if item.status is JobStatus.PENDING
                ),
                key=lambda item: item.job_id,
            )
            if not pending:
                return None
            job = pending[0]
            job.status = JobStatus.RUNNING
            job.boot_id = boot_id
            return ClaimedJob(
                job_id=job.job_id,
                run_id=job.run_id,
                boot_id=boot_id,
                status=job.status,
            )

    def reconcile_boot(self, *, current_boot_id: str) -> int:
        with self._lock:
            reconciled = 0
            for job in self._jobs.values():
                if (
                    job.status is JobStatus.RUNNING
                    and job.boot_id != current_boot_id
                ):
                    job.status = JobStatus.PENDING
                    job.boot_id = None
                    reconciled += 1
            if reconciled:
                self.audit_count += reconciled
            return reconciled

    def get_job_context(self, *, run_id: str) -> AgentJobContext:
        with self._lock:
            registration = self._runs.get(run_id)
            if registration is None:
                raise ServerError(ServerErrorCode.DISCOVERY_NOT_FOUND)
            discovery = self._discoveries[registration.discovery_id]
            return AgentJobContext(registration, discovery)

    def settle_job(
        self,
        *,
        job_id: str,
        boot_id: str,
        succeeded: bool,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if (
                job is None
                or job.status is not JobStatus.RUNNING
                or job.boot_id != boot_id
            ):
                raise ServerError(ServerErrorCode.JOB_NOT_FOUND)
            job.status = (
                JobStatus.COMPLETED if succeeded else JobStatus.FAILED
            )
