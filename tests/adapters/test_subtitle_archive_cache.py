from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from reeloom.adapters.subtitle_archive_cache import (
    FilesystemSubtitleArchiveCache,
)
from reeloom.kernel.subtitle_acquisition import (
    SubtitleArchiveFormat,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleArchiveVolume,
    SubtitleReleaseId,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.subtitle_acquisition import (
    DownloadedArchiveVolume,
    DownloadedSubtitleArchiveSet,
    SubtitleArchiveError,
    SubtitleArchiveErrorCode,
)


_CONTENT = b"PK\x03\x04cached archive"


def _downloaded(path: Path) -> DownloadedSubtitleArchiveSet:
    metadata = path.stat()
    capability = SubtitleArchiveSetCapability(
        SubtitleArchiveSetId(1),
        SubtitleReleaseId(1),
        SubtitleArchiveFormat.ZIP,
        10081,
        95257,
        (34768,),
        len(_CONTENT),
    )
    volume = SubtitleArchiveVolume(
        1,
        34768,
        len(_CONTENT),
        hashlib.sha256(_CONTENT).hexdigest(),
    )
    return DownloadedSubtitleArchiveSet(
        capability,
        (
            DownloadedArchiveVolume(
                volume,
                path,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            ),
        ),
    )


def _cache(tmp_path: Path) -> FilesystemSubtitleArchiveCache:
    root = tmp_path / "cache"
    root.mkdir()
    return FilesystemSubtitleArchiveCache(AuthorizedRoot.create(root))


def test_cache_persists_and_loads_verified_content(tmp_path: Path) -> None:
    source = tmp_path / "download.bin"
    source.write_bytes(_CONTENT)
    downloaded = _downloaded(source)
    cache = _cache(tmp_path)

    stored = cache.store(downloaded)
    source.write_bytes(b"changed")
    loaded = cache.load(
        downloaded.capability,
        tuple(item.volume for item in downloaded.volumes),
    )

    assert loaded == stored
    assert loaded is not None
    assert loaded.volumes[0].path.read_bytes() == _CONTENT
    assert loaded.volumes[0].path.parent == cache.cache_root


def test_cache_store_is_idempotent_and_never_overwrites(tmp_path: Path) -> None:
    source = tmp_path / "download.bin"
    source.write_bytes(_CONTENT)
    downloaded = _downloaded(source)
    cache = _cache(tmp_path)

    first = cache.store(downloaded)
    second = cache.store(downloaded)

    assert first.volumes[0].path == second.volumes[0].path
    assert first.volumes[0].path.read_bytes() == _CONTENT


def test_cache_returns_none_when_verified_content_is_missing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "download.bin"
    source.write_bytes(_CONTENT)
    downloaded = _downloaded(source)
    cache = _cache(tmp_path)

    assert cache.load(
        downloaded.capability,
        tuple(item.volume for item in downloaded.volumes),
    ) is None


def test_cache_rejects_corrupt_existing_content_without_overwrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "download.bin"
    source.write_bytes(_CONTENT)
    downloaded = _downloaded(source)
    cache = _cache(tmp_path)
    name = "archive-volume-sha256-" + hashlib.sha256(_CONTENT).hexdigest()
    collision = cache.cache_root / name
    collision.write_bytes(b"corrupt")

    with pytest.raises(SubtitleArchiveError) as raised:
        cache.store(downloaded)

    assert raised.value.code is SubtitleArchiveErrorCode.CONTENT_DRIFT
    assert collision.read_bytes() == b"corrupt"


def test_cache_rejects_symlink_entry_without_following(tmp_path: Path) -> None:
    source = tmp_path / "download.bin"
    source.write_bytes(_CONTENT)
    downloaded = _downloaded(source)
    cache = _cache(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(_CONTENT)
    name = "archive-volume-sha256-" + hashlib.sha256(_CONTENT).hexdigest()
    (cache.cache_root / name).symlink_to(outside)

    with pytest.raises(SubtitleArchiveError):
        cache.load(
            downloaded.capability,
            tuple(item.volume for item in downloaded.volumes),
        )

    assert outside.read_bytes() == _CONTENT


def test_cache_ignores_persisted_stat_identity(tmp_path: Path) -> None:
    source = tmp_path / "download.bin"
    source.write_bytes(_CONTENT)
    downloaded = _downloaded(source)
    changed_identity = DownloadedSubtitleArchiveSet(
        downloaded.capability,
        (
            DownloadedArchiveVolume(
                downloaded.volumes[0].volume,
                source,
                999,
                999,
                999,
                999,
            ),
        ),
    )
    cache = _cache(tmp_path)

    stored = cache.store(changed_identity)

    assert stored.volumes[0].path.read_bytes() == _CONTENT
