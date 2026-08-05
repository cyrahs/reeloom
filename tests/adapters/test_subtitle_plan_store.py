from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from reeloom.adapters.subtitle_plan_store import (
    FilesystemSubtitleAcquisitionPlanStore,
)
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
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


def _plan() -> SubtitleAcquisitionPlan:
    content = b"subtitle"
    archive = b"PK\x03\x04archive"
    archive_id = SubtitleArchiveSetId(1)
    return SubtitleAcquisitionPlan.create(
        run_id="run-m13-store",
        config_revision_id="config-1",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        source_root=RootBinding(PurePosixPath("/media"), 1, 2),
        source_folder="release",
        source_folder_device=1,
        source_folder_inode=3,
        folder_generation_id="generation-1",
        candidate_snapshot_id="candidate-snapshot-v1:" + "a" * 64,
        tmdb_id=123,
        archives=(
            SubtitleArchiveSource(
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
                        len(archive),
                        hashlib.sha256(archive).hexdigest(),
                    ),
                ),
            ),
        ),
        inspected_members=(
            InspectedSubtitleMember(
                archive_id,
                PurePosixPath("E01.ass"),
                len(content),
                hashlib.sha256(content).hexdigest(),
            ),
        ),
    )


def test_subtitle_plan_store_is_write_once_and_round_trips(tmp_path) -> None:
    root = tmp_path / "plans"
    root.mkdir()
    store = FilesystemSubtitleAcquisitionPlanStore(AuthorizedRoot.create(root))
    plan = _plan()

    store.save(plan)

    assert store.load(plan.plan_hash) == plan.canonical_bytes()
    with pytest.raises(ExecutorError) as raised:
        store.save(plan)
    assert raised.value.code is ExecutorErrorCode.PLAN_ALREADY_EXISTS


def test_subtitle_plan_store_detects_content_tampering(tmp_path) -> None:
    root = tmp_path / "plans"
    root.mkdir()
    store = FilesystemSubtitleAcquisitionPlanStore(AuthorizedRoot.create(root))
    plan = _plan()
    store.save(plan)
    stored = next(root.iterdir())
    stored.write_bytes(plan.canonical_bytes() + b" ")

    with pytest.raises(ExecutorError) as raised:
        store.load(plan.plan_hash)
    assert raised.value.code is ExecutorErrorCode.INVALID_PLAN


def test_subtitle_plan_store_never_follows_symlink(tmp_path) -> None:
    root = tmp_path / "plans"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    plan = _plan()
    name = (
        "subtitle-acquisition-v1-"
        f"{plan.plan_hash.removeprefix('sha256:')}.json"
    )
    (root / name).symlink_to(outside)
    store = FilesystemSubtitleAcquisitionPlanStore(AuthorizedRoot.create(root))

    with pytest.raises(ExecutorError) as raised:
        store.load(plan.plan_hash)
    assert raised.value.code is ExecutorErrorCode.PLAN_STORE_FAILURE
