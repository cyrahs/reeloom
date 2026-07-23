from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.scanner import ScannedFile, build_candidate_snapshot


def test_snapshot_ids_are_stable_and_per_kind() -> None:
    files = (
        ScannedFile(PurePosixPath("z/02.SRT"), CandidateKind.SUBTITLE, 2),
        ScannedFile(PurePosixPath("b.mkv"), CandidateKind.VIDEO, 3),
        ScannedFile(PurePosixPath("A.MP4"), CandidateKind.VIDEO, 4),
    )

    first = build_candidate_snapshot(files)
    second = build_candidate_snapshot(reversed(files))

    assert first == second
    assert first.snapshot_id == second.snapshot_id
    assert tuple(
        (str(candidate.id), candidate.display_name)
        for candidate in first.candidates.candidates
    ) == (
        ("video:1", "A.MP4"),
        ("video:2", "b.mkv"),
        ("subtitle:1", "z/02.SRT"),
    )


def test_snapshot_keeps_exact_path_internal() -> None:
    result = build_candidate_snapshot(
        [
            ScannedFile(
                PurePosixPath("nested/episode.mkv"),
                CandidateKind.VIDEO,
                10,
            )
        ]
    )

    record = result.record_for(
        CandidateId(CandidateKind.VIDEO, 1)
    )

    assert record.relative_path == PurePosixPath("nested/episode.mkv")
    assert record.size_bytes == 10


def test_duplicate_scanned_path_fails_closed() -> None:
    item = ScannedFile(
        PurePosixPath("episode.mkv"),
        CandidateKind.VIDEO,
        10,
    )

    with pytest.raises(DomainError) as error:
        build_candidate_snapshot([item, item])

    assert error.value.code is ErrorCode.DUPLICATE_SCANNED_PATH


def test_scanned_file_rejects_env_component_without_io() -> None:
    with pytest.raises(DomainError) as error:
        ScannedFile(
            PurePosixPath("nested/.env-video.mkv"),
            CandidateKind.VIDEO,
            10,
        )

    assert error.value.code is ErrorCode.ENV_PATH_FORBIDDEN
