from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import PurePosixPath

import pytest

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.semantic_identity import (
    SemanticCandidateSnapshot,
    SemanticRootBinding,
    SemanticSourceIdentity,
)


def _video(
    *,
    path: str = "release/episode.mkv",
    size: int = 1_024,
) -> SemanticSourceIdentity:
    return SemanticSourceIdentity(
        candidate_id=CandidateId(CandidateKind.VIDEO, 1),
        kind=CandidateKind.VIDEO,
        relative_path=PurePosixPath(path),
        size_bytes=size,
    )


def _subtitle(
    *,
    path: str = "release/episode.ass",
    digest: str = "a" * 64,
) -> SemanticSourceIdentity:
    return SemanticSourceIdentity(
        candidate_id=CandidateId(CandidateKind.SUBTITLE, 1),
        kind=CandidateKind.SUBTITLE,
        relative_path=PurePosixPath(path),
        size_bytes=128,
        sha256=digest,
    )


def test_video_identity_contains_only_semantic_file_state() -> None:
    payload = _video().payload()

    assert payload == {
        "candidate_id": "video:1",
        "file_type": "regular",
        "kind": "video",
        "relative_path": "release/episode.mkv",
        "size_bytes": 1_024,
    }
    assert {"device", "inode", "mtime", "mtime_ns", "ctime", "ctime_ns"}.isdisjoint(
        payload
    )
    assert "sha256" not in payload

    payload["mtime_ns"] = 99
    with pytest.raises(DomainError):
        SemanticSourceIdentity.from_payload(payload)


def test_same_path_and_size_video_replacement_is_intentionally_indistinguishable() -> None:
    before = SemanticCandidateSnapshot.create((_video(),))
    after = SemanticCandidateSnapshot.create((_video(),))

    assert before.snapshot_id == after.snapshot_id
    assert before == after


def test_subtitle_identity_requires_and_round_trips_full_sha256() -> None:
    subtitle = _subtitle()

    assert SemanticSourceIdentity.from_payload(subtitle.payload()) == subtitle
    assert subtitle.payload()["sha256"] == "a" * 64

    for digest in (None, "A" * 64, "a" * 63, "g" * 64):
        with pytest.raises(DomainError) as raised:
            _subtitle(digest=digest)  # type: ignore[arg-type]
        assert raised.value.code is ErrorCode.INCOMPLETE_SOURCE_IDENTITY


def test_video_identity_rejects_content_hash_and_non_regular_payload() -> None:
    with pytest.raises(DomainError) as raised:
        SemanticSourceIdentity(
            candidate_id=CandidateId(CandidateKind.VIDEO, 1),
            kind=CandidateKind.VIDEO,
            relative_path=PurePosixPath("release/episode.mkv"),
            size_bytes=1_024,
            sha256="a" * 64,
        )
    assert raised.value.code is ErrorCode.INVALID_FIELD_TYPE

    payload = _video().payload()
    payload["file_type"] = "symlink"
    with pytest.raises(DomainError) as raised:
        SemanticSourceIdentity.from_payload(payload)
    assert raised.value.code is ErrorCode.INVALID_FIELD_TYPE


def test_snapshot_is_order_independent_and_changes_with_semantic_state() -> None:
    original = SemanticCandidateSnapshot.create((_video(), _subtitle()))
    reordered = SemanticCandidateSnapshot.create((_subtitle(), _video()))
    changed_size = SemanticCandidateSnapshot.create(
        (_video(size=1_025), _subtitle())
    )
    changed_path = SemanticCandidateSnapshot.create(
        (_video(path="release/replaced.mkv"), _subtitle())
    )
    changed_hash = SemanticCandidateSnapshot.create(
        (_video(), _subtitle(digest="b" * 64))
    )

    assert original == reordered
    assert original.snapshot_id == reordered.snapshot_id
    assert len(
        {
            original.snapshot_id,
            changed_size.snapshot_id,
            changed_path.snapshot_id,
            changed_hash.snapshot_id,
        }
    ) == 4


def test_snapshot_round_trip_rejects_hash_drift_and_duplicate_paths() -> None:
    snapshot = SemanticCandidateSnapshot.create((_video(), _subtitle()))

    assert (
        SemanticCandidateSnapshot.from_payload(
            snapshot.payload(), snapshot_id=snapshot.snapshot_id
        )
        == snapshot
    )
    with pytest.raises(DomainError) as raised:
        SemanticCandidateSnapshot.from_payload(
            snapshot.payload(),
            snapshot_id="candidate-snapshot-v2:" + "0" * 64,
        )
    assert raised.value.code is ErrorCode.PLAN_MAPPING_MISMATCH

    duplicate_path = SemanticSourceIdentity(
        candidate_id=CandidateId(CandidateKind.VIDEO, 2),
        kind=CandidateKind.VIDEO,
        relative_path=_video().relative_path,
        size_bytes=5,
    )
    with pytest.raises(DomainError) as raised:
        SemanticCandidateSnapshot.create((_video(), duplicate_path))
    assert raised.value.code is ErrorCode.DUPLICATE_SCANNED_PATH


@pytest.mark.parametrize(
    "path,code",
    (
        (PurePosixPath("/absolute.mkv"), ErrorCode.PATH_ESCAPE),
        (PurePosixPath("release/../episode.mkv"), ErrorCode.PATH_ESCAPE),
        (PurePosixPath("release/.env.production"), ErrorCode.ENV_PATH_FORBIDDEN),
    ),
)
def test_semantic_source_rejects_unsafe_paths(
    path: PurePosixPath,
    code: ErrorCode,
) -> None:
    with pytest.raises(DomainError) as raised:
        SemanticSourceIdentity(
            candidate_id=CandidateId(CandidateKind.VIDEO, 1),
            kind=CandidateKind.VIDEO,
            relative_path=path,
            size_bytes=1,
        )
    assert raised.value.code is code


def test_root_binding_contains_only_the_authorized_path_and_is_frozen() -> None:
    root = SemanticRootBinding(PurePosixPath("/media/incoming"))

    assert root.payload() == {"path": "/media/incoming"}
    assert SemanticRootBinding.from_payload(root.payload()) == root
    with pytest.raises(FrozenInstanceError):
        root.path = PurePosixPath("/other")  # type: ignore[misc]

    for extra in ("device", "inode", "mount_id", "file_type"):
        payload: dict[str, object] = root.payload()
        payload[extra] = 1
        with pytest.raises(DomainError):
            SemanticRootBinding.from_payload(payload)
