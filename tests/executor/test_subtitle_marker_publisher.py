from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from reeloom.executor.subtitle_publication import (
    SubtitleMarkerPublisher,
    SubtitlePublicationState,
)
from reeloom.kernel.subtitle_publication import (
    SUBTITLE_PUBLICATION_MARKER,
    SubtitlePublicationManifest,
    SubtitlePublicationMember,
)
from reeloom.policy.path_policy import AuthorizedRoot


_CONTENT = b"[Script Info]\nTitle: fixed\n"


def _member() -> SubtitlePublicationMember:
    return SubtitlePublicationMember(
        name="episode.ass",
        size_bytes=len(_CONTENT),
        sha256=hashlib.sha256(_CONTENT).hexdigest(),
    )


def _manifest() -> SubtitlePublicationManifest:
    return SubtitlePublicationManifest.create(
        plan_hash="sha256:" + "a" * 64,
        publication_directory="reeloom-acquired-" + "a" * 64,
        members=(_member(),),
    )


@dataclass
class _ContentSource:
    content: bytes = _CONTENT
    calls: int = 0

    async def read_member(self, member: SubtitlePublicationMember) -> bytes:
        self.calls += 1
        return self.content


def _publish(
    tmp_path: Path,
    source: _ContentSource,
):
    root = tmp_path / "media"
    (root / "release").mkdir(parents=True, exist_ok=True)
    result = asyncio.run(
        SubtitleMarkerPublisher().publish(
            root=AuthorizedRoot.create(root),
            source_folder="release",
            manifest=_manifest(),
            content_source=source,
        )
    )
    return result, root / "release" / _manifest().publication_directory


def test_publisher_writes_members_then_complete_marker(tmp_path: Path) -> None:
    source = _ContentSource()

    result, destination = _publish(tmp_path, source)

    assert result.state is SubtitlePublicationState.COMPLETED
    assert result.published_count == 1
    assert source.calls == 1
    assert (destination / "episode.ass").read_bytes() == _CONTENT
    assert (destination / SUBTITLE_PUBLICATION_MARKER).read_bytes() == (
        _manifest().canonical_bytes()
    )


def test_publisher_reuses_completed_publication_without_reading_content(
    tmp_path: Path,
) -> None:
    first_source = _ContentSource()
    _publish(tmp_path, first_source)
    second_source = _ContentSource()

    result, _ = _publish(tmp_path, second_source)

    assert result.state is SubtitlePublicationState.COMPLETED
    assert result.published_count == 1
    assert second_source.calls == 0


def test_publisher_converges_exact_partial_member(tmp_path: Path) -> None:
    root = tmp_path / "media"
    destination = root / "release" / _manifest().publication_directory
    destination.mkdir(parents=True)
    (destination / "episode.ass").write_bytes(_CONTENT)
    source = _ContentSource()

    result, _ = _publish(tmp_path, source)

    assert result.state is SubtitlePublicationState.COMPLETED
    assert source.calls == 0
    assert (destination / SUBTITLE_PUBLICATION_MARKER).is_file()


def test_publisher_never_overwrites_mismatching_partial_member(
    tmp_path: Path,
) -> None:
    root = tmp_path / "media"
    destination = root / "release" / _manifest().publication_directory
    destination.mkdir(parents=True)
    member = destination / "episode.ass"
    member.write_bytes(b"different")

    result, _ = _publish(tmp_path, _ContentSource())

    assert result.state is SubtitlePublicationState.COLLISION
    assert result.reason == "member_mismatch"
    assert member.read_bytes() == b"different"
    assert not (destination / SUBTITLE_PUBLICATION_MARKER).exists()


def test_publisher_rejects_unexpected_entry(tmp_path: Path) -> None:
    root = tmp_path / "media"
    destination = root / "release" / _manifest().publication_directory
    destination.mkdir(parents=True)
    (destination / "unexpected.txt").write_text("foreign")

    result, _ = _publish(tmp_path, _ContentSource())

    assert result.state is SubtitlePublicationState.COLLISION
    assert result.reason == "unexpected_entry"


def test_publisher_rejects_symlink_member(tmp_path: Path) -> None:
    root = tmp_path / "media"
    destination = root / "release" / _manifest().publication_directory
    destination.mkdir(parents=True)
    outside = tmp_path / "outside.ass"
    outside.write_bytes(_CONTENT)
    (destination / "episode.ass").symlink_to(outside)

    result, _ = _publish(tmp_path, _ContentSource())

    assert result.state is SubtitlePublicationState.UNSAFE
    assert outside.read_bytes() == _CONTENT


def test_publisher_treats_fsync_failure_as_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsupported_fsync(_: int) -> None:
        raise OSError("unsupported")

    monkeypatch.setattr(os, "fsync", unsupported_fsync)

    result, destination = _publish(tmp_path, _ContentSource())

    assert result.state is SubtitlePublicationState.COMPLETED
    assert result.warnings == (
        "file_fsync_unavailable",
        "file_fsync_unavailable",
        "directory_fsync_unavailable",
    )
    assert (destination / SUBTITLE_PUBLICATION_MARKER).is_file()
