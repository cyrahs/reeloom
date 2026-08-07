from __future__ import annotations

import hashlib
import json

import pytest

from reeloom.kernel.errors import DomainError
from reeloom.kernel.subtitle_publication import (
    SubtitlePublicationManifest,
    SubtitlePublicationMember,
)


def _member(name: str = "episode.ass", content: bytes = b"subtitle") -> SubtitlePublicationMember:
    return SubtitlePublicationMember(
        name=name,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _manifest(
    *members: SubtitlePublicationMember,
) -> SubtitlePublicationManifest:
    plan_hash = "sha256:" + "a" * 64
    return SubtitlePublicationManifest.create(
        plan_hash=plan_hash,
        publication_directory="reeloom-acquired-" + "a" * 64,
        members=members or (_member(),),
    )


def test_publication_manifest_is_canonical_and_strict() -> None:
    manifest = _manifest(_member("z.ass"), _member("a.srt", b"other"))

    restored = SubtitlePublicationManifest.from_canonical_bytes(
        manifest.canonical_bytes()
    )

    assert restored == manifest
    assert tuple(item.name for item in restored.members) == ("a.srt", "z.ass")
    assert len(restored.digest) == 64


@pytest.mark.parametrize(
    "name",
    ["../escape.ass", "nested/episode.ass", ".env-subtitle", "a\\b.ass"],
)
def test_publication_member_rejects_unsafe_name(name: str) -> None:
    with pytest.raises(DomainError):
        _member(name)


def test_publication_manifest_rejects_casefold_collision() -> None:
    with pytest.raises(DomainError):
        _manifest(_member("Episode.ass"), _member("episode.ass"))


def test_publication_manifest_rejects_wrong_plan_directory_binding() -> None:
    with pytest.raises(DomainError):
        SubtitlePublicationManifest.create(
            plan_hash="sha256:" + "a" * 64,
            publication_directory="reeloom-acquired-" + "b" * 64,
            members=(_member(),),
        )


def test_publication_manifest_rejects_noncanonical_marker() -> None:
    payload = json.loads(_manifest().canonical_bytes())
    content = json.dumps(payload, indent=2).encode()

    with pytest.raises(DomainError):
        SubtitlePublicationManifest.from_canonical_bytes(content)
