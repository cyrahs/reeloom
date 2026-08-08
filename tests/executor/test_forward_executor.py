from __future__ import annotations

import errno
import os
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from reeloom.adapters.forward_filesystem import PosixForwardFilesystem
from reeloom.executor.forward import ForwardExecutor
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.forward_execution import (
    ExecutionItemOutcome,
    ExecutionOperation,
    ExecutionOperationLease,
    ExecutionOperationStatus,
    PathObservationState,
    RenamePlanV2,
    compile_plan_draft_v2,
)
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.naming import SeriesIdentity
from reeloom.kernel.semantic_identity import (
    SemanticCandidateSnapshot,
    SemanticRootBinding,
    SemanticSourceIdentity,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.ports.forward_filesystem import (
    ForwardMoveDiagnostic,
    ForwardMoveEffect,
)

_NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _plan(
    *,
    source_root: str = "/source",
    output_root: str = "/output",
    count: int = 1,
) -> RenamePlanV2:
    sources = tuple(
        SemanticSourceIdentity(
            candidate_id=CandidateId(CandidateKind.VIDEO, index),
            kind=CandidateKind.VIDEO,
            relative_path=PurePosixPath(f"Work/episode-{index}.mkv"),
            size_bytes=index * 10,
        )
        for index in range(1, count + 1)
    )
    snapshot = SemanticCandidateSnapshot.create(sources)
    mapping = MappingDraft.from_dict(
        {
            "videos": [
                {
                    "video_id": f"video:{index}",
                    "season": 1,
                    "episode_start": index,
                    "episode_end": index,
                }
                for index in range(1, count + 1)
            ],
            "subtitles": [],
        },
        candidates=snapshot.candidates,
        catalog=EpisodeCatalog.from_counts({1: count}),
    )
    draft = compile_plan_draft_v2(
        series=SeriesIdentity("Series", 2026, 14),
        mapping=mapping,
        candidates=snapshot,
        subtitle_variants=(),
    )
    return RenamePlanV2.create(
        run_id="run-m14",
        config_revision=1,
        watch_id="watch-anime",
        work_type=TmdbWorkType.ANIME,
        created_at=_NOW,
        source_root=SemanticRootBinding(PurePosixPath(source_root)),
        output_root=SemanticRootBinding(PurePosixPath(output_root)),
        candidate_snapshot=snapshot,
        subtitle_variants=(),
        draft=draft,
    )


def _lease(
    plan: RenamePlanV2,
    *,
    operation: ExecutionOperation | None = None,
) -> ExecutionOperationLease:
    current = operation or ExecutionOperation.authorized(
        operation_id="operation:m14",
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
    )
    return ExecutionOperationLease.issue(
        current,
        worker_id="worker:1",
        now=_NOW,
        lease_for=timedelta(minutes=5),
    )


@dataclass(slots=True)
class SemanticFakeFilesystem:
    files: dict[
        tuple[str, str], SemanticSourceIdentity
    ] = field(default_factory=dict)
    visibility_delay_reads: int = 0
    transient_unavailable_reads: int = 0
    post_move_unavailable_reads: int = 0
    rename_error_after_effect: bool = False
    move_without_effect: bool = False
    native_noreplace_supported: bool = True
    directory_fsync_supported: bool = True
    chmod_effective: bool = True
    unstable_stat_identity: bool = False
    metadata_tick: int = 0
    _visible: dict[tuple[str, str], tuple[PathObservationState, int]] = (
        field(default_factory=dict)
    )

    @staticmethod
    def _key(root: SemanticRootBinding, path: PurePosixPath) -> tuple[str, str]:
        return root.path.as_posix(), path.as_posix()

    def put(
        self,
        root: SemanticRootBinding,
        path: PurePosixPath,
        identity: SemanticSourceIdentity,
    ) -> None:
        self.files[self._key(root, path)] = identity

    def observe(
        self,
        *,
        root: SemanticRootBinding,
        relative_path: PurePosixPath,
        expected: SemanticSourceIdentity,
    ) -> PathObservationState:
        self.metadata_tick += 1
        key = self._key(root, relative_path)
        scripted = self._visible.get(key)
        if scripted is not None:
            state, remaining = scripted
            if remaining > 1:
                self._visible[key] = (state, remaining - 1)
            else:
                self._visible.pop(key)
            return state
        if self.transient_unavailable_reads > 0:
            self.transient_unavailable_reads -= 1
            return PathObservationState.UNAVAILABLE
        actual = self.files.get(key)
        if actual is None:
            return PathObservationState.ABSENT
        return (
            PathObservationState.MATCHING
            if (
                actual.kind is expected.kind
                and actual.size_bytes == expected.size_bytes
                and actual.sha256 == expected.sha256
            )
            else PathObservationState.MISMATCHED
        )

    def move(
        self,
        *,
        source_root: SemanticRootBinding,
        source_path: PurePosixPath,
        expected: SemanticSourceIdentity,
        destination_root: SemanticRootBinding,
        destination_path: PurePosixPath,
    ) -> ForwardMoveEffect:
        source_key = self._key(source_root, source_path)
        destination_key = self._key(destination_root, destination_path)
        if destination_key in self.files:
            return ForwardMoveEffect(ForwardMoveDiagnostic.COLLISION)
        if self.move_without_effect:
            return ForwardMoveEffect(ForwardMoveDiagnostic.TRANSIENT_IO)
        actual = self.files.pop(source_key, None)
        if actual is None or actual.size_bytes != expected.size_bytes:
            return ForwardMoveEffect(ForwardMoveDiagnostic.TRANSIENT_IO)
        self.files[destination_key] = actual
        self.transient_unavailable_reads = (
            self.post_move_unavailable_reads
        )
        if self.visibility_delay_reads:
            self._visible[source_key] = (
                PathObservationState.MATCHING,
                self.visibility_delay_reads,
            )
            self._visible[destination_key] = (
                PathObservationState.ABSENT,
                self.visibility_delay_reads,
            )
        diagnostic = (
            ForwardMoveDiagnostic.TRANSIENT_IO
            if self.rename_error_after_effect
            else (
                ForwardMoveDiagnostic.NATIVE
                if self.native_noreplace_supported
                else ForwardMoveDiagnostic.CHECKED_RENAME
            )
        )
        warnings = (
            ()
            if self.directory_fsync_supported
            else ("directory_fsync_unsupported",)
        )
        return ForwardMoveEffect(diagnostic, warnings)


def _seed(fake: SemanticFakeFilesystem, plan: RenamePlanV2) -> None:
    for source in plan.sources:
        fake.put(plan.source_root, source.relative_path, source)


def _executor(fake: SemanticFakeFilesystem) -> ForwardExecutor:
    return ForwardExecutor(
        fake,
        clock=lambda: _NOW + timedelta(seconds=1),
        sleeper=lambda _delay: None,
    )


@pytest.mark.parametrize(
    "configuration",
    (
        {"rename_error_after_effect": True},
        {"visibility_delay_reads": 2},
        {"native_noreplace_supported": False},
        {"directory_fsync_supported": False},
        {
            "post_move_unavailable_reads": 3,
            "unstable_stat_identity": True,
            "chmod_effective": False,
        },
    ),
)
def test_forward_executor_uses_final_state_as_truth(
    configuration: dict[str, object],
) -> None:
    plan = _plan()
    fake = SemanticFakeFilesystem(**configuration)
    _seed(fake, plan)

    result = _executor(fake).execute(plan, _lease(plan))

    assert result.operation.status is ExecutionOperationStatus.COMPLETED
    assert result.items[0].outcome is ExecutionItemOutcome.SATISFIED
    assert not result.fresh_scan_required
    if configuration.get("directory_fsync_supported") is False:
        assert result.warnings == ("directory_fsync_unsupported",)


def test_forward_executor_continues_after_independent_collision() -> None:
    plan = _plan(count=2)
    fake = SemanticFakeFilesystem()
    _seed(fake, plan)
    first_move = plan.draft.moves[0]
    fake.put(
        plan.output_root,
        first_move.destination,
        plan.sources[0],
    )

    result = _executor(fake).execute(plan, _lease(plan))

    assert result.operation.status is ExecutionOperationStatus.PARTIAL
    assert result.operation.outcomes == (
        ExecutionItemOutcome.COLLISION,
        ExecutionItemOutcome.SATISFIED,
    )
    assert result.fresh_scan_required
    second_move = plan.draft.moves[1]
    assert (
        fake.observe(
            root=plan.output_root,
            relative_path=second_move.destination,
            expected=plan.sources[1],
        )
        is PathObservationState.MATCHING
    )


def test_forward_executor_reconciles_after_effect_without_db_settlement() -> None:
    plan = _plan()
    fake = SemanticFakeFilesystem()
    _seed(fake, plan)
    first_lease = _lease(plan)

    first_result = _executor(fake).execute(plan, first_lease)
    replay_lease = _lease(plan, operation=first_lease.operation)
    replay = _executor(fake).execute(plan, replay_lease)

    assert first_result.operation.status is ExecutionOperationStatus.COMPLETED
    assert replay.operation.status is ExecutionOperationStatus.COMPLETED
    assert replay.operation.attempt_count == 2
    assert replay.items[0].diagnostic is None


def test_forward_executor_retries_same_operation_after_crash_before_effect() -> None:
    plan = _plan()
    fake = SemanticFakeFilesystem()
    _seed(fake, plan)
    abandoned_lease = _lease(plan)
    replay_lease = _lease(plan, operation=abandoned_lease.operation)

    replay = _executor(fake).execute(plan, replay_lease)

    assert replay.operation.status is ExecutionOperationStatus.COMPLETED
    assert replay.operation.attempt_count == 2
    assert (
        replay.items[0].diagnostic is ForwardMoveDiagnostic.NATIVE
    )


def test_no_effect_move_terminates_unavailable_without_rollback() -> None:
    plan = _plan()
    fake = SemanticFakeFilesystem(move_without_effect=True)
    _seed(fake, plan)

    result = _executor(fake).execute(plan, _lease(plan))

    assert result.operation.status is ExecutionOperationStatus.UNAVAILABLE
    assert result.items[0].outcome is ExecutionItemOutcome.UNAVAILABLE
    assert (
        fake.observe(
            root=plan.source_root,
            relative_path=plan.sources[0].relative_path,
            expected=plan.sources[0],
        )
        is PathObservationState.MATCHING
    )


def test_posix_forward_filesystem_moves_using_path_and_size_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    work = source_root / "Work"
    work.mkdir(parents=True)
    output_root.mkdir()
    video = work / "episode-1.mkv"
    video.write_bytes(b"x" * 10)
    plan = _plan(
        source_root=source_root.as_posix(),
        output_root=output_root.as_posix(),
    )
    monkeypatch.setattr(
        "reeloom.adapters.forward_filesystem.rename_noreplace",
        lambda *_args: (_ for _ in ()).throw(
            OSError(errno.ENOSYS, "unsupported")
        ),
    )

    result = ForwardExecutor(
        PosixForwardFilesystem(),
        clock=lambda: _NOW + timedelta(seconds=1),
        sleeper=lambda _delay: None,
    ).execute(plan, _lease(plan))

    destination = output_root / Path(plan.draft.moves[0].destination)
    assert result.operation.status is ExecutionOperationStatus.COMPLETED
    assert not video.exists()
    assert destination.read_bytes() == b"x" * 10


def test_posix_final_state_wins_when_rename_reports_failure_after_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    work = source_root / "Work"
    work.mkdir(parents=True)
    output_root.mkdir()
    (work / "episode-1.mkv").write_bytes(b"x" * 10)
    plan = _plan(
        source_root=source_root.as_posix(),
        output_root=output_root.as_posix(),
    )

    def moved_then_failed(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        os.rename(
            source_name,
            destination_name,
            src_dir_fd=source_parent_fd,
            dst_dir_fd=destination_parent_fd,
        )
        raise OSError(errno.EIO, "remote result unknown")

    monkeypatch.setattr(
        "reeloom.adapters.forward_filesystem.rename_noreplace",
        moved_then_failed,
    )

    result = ForwardExecutor(
        PosixForwardFilesystem(),
        clock=lambda: _NOW + timedelta(seconds=1),
        sleeper=lambda _delay: None,
    ).execute(plan, _lease(plan))

    assert result.operation.status is ExecutionOperationStatus.COMPLETED
    assert (
        result.items[0].diagnostic
        is ForwardMoveDiagnostic.TRANSIENT_IO
    )


def test_posix_symlink_and_casefold_collision_are_never_modified(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    work = source_root / "Work"
    work.mkdir(parents=True)
    output_root.mkdir()
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"x" * 10)
    source = work / "episode-1.mkv"
    source.symlink_to(outside)
    plan = _plan(
        source_root=source_root.as_posix(),
        output_root=output_root.as_posix(),
    )

    unsafe = ForwardExecutor(
        PosixForwardFilesystem(),
        clock=lambda: _NOW + timedelta(seconds=1),
        sleeper=lambda _delay: None,
    ).execute(plan, _lease(plan))

    assert unsafe.operation.status is ExecutionOperationStatus.UNSAFE
    assert source.is_symlink()
    source.unlink()
    source.write_bytes(b"x" * 10)
    planned_parent = plan.draft.moves[0].destination.parts[0]
    (output_root / planned_parent.swapcase()).mkdir()

    collision = ForwardExecutor(
        PosixForwardFilesystem(),
        clock=lambda: _NOW + timedelta(seconds=1),
        sleeper=lambda _delay: None,
    ).execute(plan, _lease(plan))

    assert collision.operation.status is ExecutionOperationStatus.COLLISION
    assert source.read_bytes() == b"x" * 10


def test_posix_subtitle_observation_uses_full_sha256(tmp_path: Path) -> None:
    root_path = tmp_path / "root"
    root_path.mkdir()
    subtitle = root_path / "episode.ass"
    subtitle.write_bytes(b"subtitle-a")
    expected = SemanticSourceIdentity(
        candidate_id=CandidateId(CandidateKind.SUBTITLE, 1),
        kind=CandidateKind.SUBTITLE,
        relative_path=PurePosixPath("episode.ass"),
        size_bytes=len(b"subtitle-b"),
        sha256=hashlib.sha256(b"subtitle-b").hexdigest(),
    )

    observed = PosixForwardFilesystem().observe(
        root=SemanticRootBinding(PurePosixPath(root_path.as_posix())),
        relative_path=PurePosixPath("episode.ass"),
        expected=expected,
    )

    assert observed is PathObservationState.MISMATCHED


def test_directory_fsync_failure_is_only_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    work = source_root / "Work"
    work.mkdir(parents=True)
    output_root.mkdir()
    (work / "episode-1.mkv").write_bytes(b"x" * 10)
    plan = _plan(
        source_root=source_root.as_posix(),
        output_root=output_root.as_posix(),
    )
    monkeypatch.setattr(
        "reeloom.adapters.forward_filesystem.rename_noreplace",
        lambda *_args: (_ for _ in ()).throw(
            OSError(errno.ENOSYS, "unsupported")
        ),
    )
    monkeypatch.setattr(
        "reeloom.adapters.forward_filesystem.os.fsync",
        lambda _fd: (_ for _ in ()).throw(
            OSError(errno.EINVAL, "unsupported")
        ),
    )

    result = ForwardExecutor(
        PosixForwardFilesystem(),
        clock=lambda: _NOW + timedelta(seconds=1),
        sleeper=lambda _delay: None,
    ).execute(plan, _lease(plan))

    assert result.operation.status is ExecutionOperationStatus.COMPLETED
    assert result.warnings == ("directory_fsync_unsupported",)
