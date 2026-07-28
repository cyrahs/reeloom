from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from reeloom.kernel.errors import DomainError
from reeloom.kernel.folder_disposition import (
    FolderDispositionAction,
    FolderDispositionPlan,
)
from reeloom.kernel.rename_plan import RootBinding


def _plan() -> FolderDispositionPlan:
    return FolderDispositionPlan.create(
        run_id="run-test",
        folder_generation_id="folder-test",
        created_at=datetime(2026, 7, 27, tzinfo=UTC),
        source_root=RootBinding(PurePosixPath("/watch"), 1, 2),
        source_folder="Incoming",
        folder_device=1,
        folder_inode=3,
        inventory_id="folder-inventory-v1:" + "a" * 64,
        action=FolderDispositionAction.ARCHIVE,
        target_relative=PurePosixPath("archive/Incoming"),
        media_plan_hash="sha256:" + "b" * 64,
        file_count=2,
        reason_code="media_completed",
    )


def test_folder_disposition_round_trip_is_canonical() -> None:
    plan = _plan()

    decoded = FolderDispositionPlan.from_canonical_bytes(
        plan.canonical_bytes()
    )

    assert decoded == plan
    assert decoded.verify_hash()


def test_folder_disposition_rejects_duplicate_key() -> None:
    content = _plan().canonical_bytes().replace(
        b'{"action":"archive",',
        b'{"action":"archive","action":"fail",',
    )

    with pytest.raises(DomainError):
        FolderDispositionPlan.from_canonical_bytes(content)


def test_remove_empty_cannot_bind_a_target() -> None:
    with pytest.raises(DomainError):
        FolderDispositionPlan.create(
            run_id="run-test",
            folder_generation_id="folder-test",
            created_at=datetime(2026, 7, 27, tzinfo=UTC),
            source_root=RootBinding(PurePosixPath("/watch"), 1, 2),
            source_folder="Incoming",
            folder_device=1,
            folder_inode=3,
            inventory_id="folder-inventory-v1:" + "a" * 64,
            action=FolderDispositionAction.REMOVE_EMPTY,
            target_relative=PurePosixPath("archive/Incoming"),
            media_plan_hash=None,
            file_count=0,
            reason_code="media_completed",
        )
