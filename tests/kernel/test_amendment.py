from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from reeloom.kernel.amendment import (
    CompletedLayout,
    CompletedLayoutFile,
    DesiredLayoutMove,
    compile_amendment,
)
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError
from reeloom.kernel.rename_plan import RootBinding
from reeloom.executor.manifest import ExecutionManifest


def _layout() -> CompletedLayout:
    return CompletedLayout(
        run_id="run-1",
        original_plan_hash="sha256:" + "a" * 64,
        transaction_id="txn-v1-" + "b" * 64,
        root=RootBinding(PurePosixPath("/archive"), 1, 2),
        files=(
            CompletedLayoutFile(
                candidate_id=CandidateId(CandidateKind.VIDEO, 1),
                kind=CandidateKind.VIDEO,
                relative_path=PurePosixPath("Series/S01/Series - S01E01.mkv"),
                size_bytes=5,
                device=1,
                inode=10,
                mtime_ns=20,
                ctime_ns=30,
                sample_digest=None,
            ),
        ),
    )


def test_completed_layout_rejects_an_empty_file_set() -> None:
    layout = _layout()

    with pytest.raises(DomainError):
        CompletedLayout(
            run_id=layout.run_id,
            original_plan_hash=layout.original_plan_hash,
            transaction_id=layout.transaction_id,
            root=layout.root,
            files=(),
        )


def test_noop_reapply_produces_no_plan() -> None:
    layout = _layout()
    desired = (
        DesiredLayoutMove(
            source_id=layout.files[0].candidate_id,
            video_id=layout.files[0].candidate_id,
            destination=layout.files[0].relative_path,
            season=1,
            episode_start=1,
            episode_end=1,
        ),
    )

    assert compile_amendment(
        layout=layout,
        desired=desired,
        created_at=datetime(2026, 7, 25, tzinfo=UTC),
    ) is None


def test_amendment_is_content_addressed_and_binds_completed_identity() -> None:
    layout = _layout()
    desired = (
        DesiredLayoutMove(
            source_id=layout.files[0].candidate_id,
            video_id=layout.files[0].candidate_id,
            destination=PurePosixPath(
                "Series/S00/Series - S00E01.mkv"
            ),
            season=0,
            episode_start=1,
            episode_end=1,
        ),
    )

    plan = compile_amendment(
        layout=layout,
        desired=desired,
        created_at=datetime(2026, 7, 25, tzinfo=UTC),
    )

    assert plan is not None
    assert plan.parent_plan_hash == layout.original_plan_hash
    assert plan.completed_transaction_id == layout.transaction_id
    assert plan.source_root == plan.output_root == layout.root
    assert plan.verify_hash()
    assert plan.moves[0].destination == desired[0].destination
    manifest = ExecutionManifest.from_canonical_bytes(
        plan.canonical_bytes(),
        plan_hash=plan.plan_hash,
    )
    assert manifest.plan_hash == plan.plan_hash
    assert manifest.source_root == manifest.output_root
    assert layout.files[0].relative_path == PurePosixPath(
        "Series/S01/Series - S01E01.mkv"
    )


def test_amendment_rejects_destination_occupied_by_other_layout_file() -> None:
    layout = _layout()
    other = CompletedLayoutFile(
        candidate_id=CandidateId(CandidateKind.VIDEO, 2),
        kind=CandidateKind.VIDEO,
        relative_path=PurePosixPath("Series/S01/Series - S01E02.mkv"),
        size_bytes=5,
        device=1,
        inode=11,
        mtime_ns=20,
        ctime_ns=30,
        sample_digest=None,
    )
    layout = CompletedLayout(
        run_id=layout.run_id,
        original_plan_hash=layout.original_plan_hash,
        transaction_id=layout.transaction_id,
        root=layout.root,
        files=layout.files + (other,),
    )

    with pytest.raises(DomainError):
        compile_amendment(
            layout=layout,
            desired=(
                DesiredLayoutMove(
                    source_id=layout.files[0].candidate_id,
                    video_id=layout.files[0].candidate_id,
                    destination=other.relative_path,
                    season=1,
                    episode_start=2,
                    episode_end=2,
                ),
                DesiredLayoutMove(
                    source_id=other.candidate_id,
                    video_id=other.candidate_id,
                    destination=other.relative_path,
                    season=1,
                    episode_start=2,
                    episode_end=2,
                ),
            ),
            created_at=datetime(2026, 7, 25, tzinfo=UTC),
        )
