from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path, PurePosixPath

import pytest

import reeloom.adapters.subtitle_archive as subtitle_archive_adapter
from reeloom.adapters.subtitle_archive import (
    FilesystemSubtitleArchiveInspector,
    FixedSevenZipRunner,
)
from reeloom.kernel.subtitle_acquisition import (
    SubtitleArchiveFormat,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleArchiveVolume,
    SubtitleReleaseId,
)
from reeloom.ports.subtitle_acquisition import (
    DownloadedArchiveVolume,
    DownloadedSubtitleArchiveSet,
    SubtitleArchiveError,
    SubtitleArchiveErrorCode,
)


class _Runner:
    def __init__(
        self,
        manifest: bytes,
        members: dict[str, bytes],
        *,
        mutate: bool = False,
    ) -> None:
        self.manifest = manifest
        self.members = members
        self.mutate = mutate

    async def list_manifest(self, archive_path: Path) -> bytes:
        if self.mutate:
            archive_path.write_bytes(archive_path.read_bytes() + b"changed")
        return self.manifest

    async def extract_member(
        self,
        archive_path: Path,
        member_path: PurePosixPath,
        *,
        max_bytes: int,
    ) -> bytes:
        return self.members[member_path.as_posix()]


def _entry(
    path: str,
    size: int,
    *,
    encrypted: str = "-",
    attributes: str = "A",
    folder: str = "-",
) -> str:
    return (
        f"Path = {path}\nSize = {size}\nPacked Size = {size}\n"
        f"Folder = {folder}\nAttributes = {attributes}\n"
        f"Encrypted = {encrypted}\nCRC = 12345678\nMethod = Store\n"
    )


def _manifest(
    archive_type: str,
    *entries: str,
    volumes: int | None = None,
) -> bytes:
    volume_field = "" if volumes is None else f"Volumes = {volumes}\n"
    return (
        "7-Zip [64] 26.02\n\nScanning the drive for archives:\n\n"
        f"Path = archive.bin\nType = {archive_type}\nPhysical Size = 16\n"
        f"{volume_field}\n----------\n" + "\n".join(entries)
    ).encode()


def _magic(format: SubtitleArchiveFormat) -> bytes:
    return {
        SubtitleArchiveFormat.ZIP: b"PK\x03\x04payload",
        SubtitleArchiveFormat.SEVEN_Z: b"7z\xbc\xaf'\x1cpayload",
        SubtitleArchiveFormat.RAR: b"Rar!\x1a\x07\x01\x00payload",
    }[format]


def _downloaded(
    tmp_path: Path,
    format: SubtitleArchiveFormat,
    *,
    volume_count: int = 1,
) -> DownloadedSubtitleArchiveSet:
    attachment_ids = tuple(range(101, 101 + volume_count))
    capability = SubtitleArchiveSetCapability(
        SubtitleArchiveSetId(1),
        SubtitleReleaseId(1),
        format,
        10081,
        95257,
        attachment_ids,
        volume_count * 16,
    )
    volumes = []
    for index, attachment_id in enumerate(attachment_ids, start=1):
        path = tmp_path / f"volume-{index}.bin"
        content = _magic(format)
        path.write_bytes(content)
        metadata = path.stat()
        volumes.append(
            DownloadedArchiveVolume(
                SubtitleArchiveVolume(
                    index,
                    attachment_id,
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                ),
                path,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
        )
    return DownloadedSubtitleArchiveSet(capability, tuple(volumes))


@pytest.mark.parametrize(
    ("format", "archive_type"),
    (
        (SubtitleArchiveFormat.ZIP, "zip"),
        (SubtitleArchiveFormat.SEVEN_Z, "7z"),
        (SubtitleArchiveFormat.RAR, "rar"),
    ),
)
def test_inspector_accepts_supported_magic_and_hashes_each_member(
    tmp_path: Path,
    format: SubtitleArchiveFormat,
    archive_type: str,
) -> None:
    content = b"dialogue"
    runner = _Runner(
        _manifest(archive_type, _entry("Subs/E01.ass", len(content))),
        {"Subs/E01.ass": content},
    )

    result = asyncio.run(
        FilesystemSubtitleArchiveInspector(runner).inspect(
            _downloaded(tmp_path, format),
            season_numbers=(1,),
        )
    )

    assert result.members[0].sha256 == hashlib.sha256(content).hexdigest()
    assert result.source.manifest_digest


def test_complete_multi_volume_rar_is_one_source(tmp_path: Path) -> None:
    content = b"subtitle"
    result = asyncio.run(
        FilesystemSubtitleArchiveInspector(
            _Runner(
                _manifest("rar", _entry("E01.srt", len(content)), volumes=2),
                {"E01.srt": content},
            )
        ).inspect(
            _downloaded(tmp_path, SubtitleArchiveFormat.RAR, volume_count=2),
            season_numbers=(1, 2),
        )
    )

    assert len(result.source.volumes) == 2
    assert result.source.season_numbers == (1, 2)


def test_missing_multi_volume_rar_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SubtitleArchiveError) as raised:
        asyncio.run(
            FilesystemSubtitleArchiveInspector(
                _Runner(
                    _manifest("rar", _entry("E01.srt", 1), volumes=1),
                    {"E01.srt": b"x"},
                )
            ).inspect(
                _downloaded(
                    tmp_path,
                    SubtitleArchiveFormat.RAR,
                    volume_count=2,
                ),
                season_numbers=(1,),
            )
        )
    assert raised.value.code is SubtitleArchiveErrorCode.INVALID_FORMAT


@pytest.mark.parametrize(
    ("entry", "expected"),
    (
        (_entry("../escape.ass", 1), SubtitleArchiveErrorCode.UNSAFE_ENTRY),
        (_entry("nested.zip", 1), SubtitleArchiveErrorCode.NESTED_ARCHIVE),
        (
            _entry("link.ass", 1, attributes="l"),
            SubtitleArchiveErrorCode.SPECIAL_FILE,
        ),
        (
            _entry("secret.ass", 1, encrypted="+"),
            SubtitleArchiveErrorCode.ENCRYPTED,
        ),
    ),
)
def test_unsafe_encrypted_special_and_nested_entries_are_fatal(
    tmp_path: Path,
    entry: str,
    expected: SubtitleArchiveErrorCode,
) -> None:
    with pytest.raises(SubtitleArchiveError) as raised:
        asyncio.run(
            FilesystemSubtitleArchiveInspector(
                _Runner(_manifest("zip", entry), {})
            ).inspect(
                _downloaded(tmp_path, SubtitleArchiveFormat.ZIP),
                season_numbers=(1,),
            )
        )
    assert raised.value.code is expected


def test_unicode_casefold_duplicate_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(
        "zip",
        _entry("Ａ.ass", 1),
        _entry("A.ass", 1),
    )
    with pytest.raises(SubtitleArchiveError) as raised:
        asyncio.run(
            FilesystemSubtitleArchiveInspector(_Runner(manifest, {})).inspect(
                _downloaded(tmp_path, SubtitleArchiveFormat.ZIP),
                season_numbers=(1,),
            )
        )
    assert raised.value.code is SubtitleArchiveErrorCode.UNSAFE_ENTRY


def test_compression_ratio_bomb_is_rejected_before_extraction(tmp_path: Path) -> None:
    with pytest.raises(SubtitleArchiveError) as raised:
        asyncio.run(
            FilesystemSubtitleArchiveInspector(
                _Runner(_manifest("zip", _entry("bomb.ass", 10_000)), {})
            ).inspect(
                _downloaded(tmp_path, SubtitleArchiveFormat.ZIP),
                season_numbers=(1,),
            )
        )
    assert raised.value.code is SubtitleArchiveErrorCode.LIMIT_EXCEEDED


def test_archive_identity_drift_during_listing_is_rejected(tmp_path: Path) -> None:
    downloaded = _downloaded(tmp_path, SubtitleArchiveFormat.ZIP)
    with pytest.raises(SubtitleArchiveError) as raised:
        asyncio.run(
            FilesystemSubtitleArchiveInspector(
                _Runner(
                    _manifest("zip", _entry("E01.ass", 1)),
                    {"E01.ass": b"x"},
                    mutate=True,
                )
            ).inspect(downloaded, season_numbers=(1,))
        )
    assert raised.value.code is SubtitleArchiveErrorCode.CONTENT_DRIFT


def test_symlink_volume_is_never_followed(tmp_path: Path) -> None:
    real = tmp_path / "real.zip"
    real.write_bytes(_magic(SubtitleArchiveFormat.ZIP))
    link = tmp_path / "link.zip"
    link.symlink_to(real)
    metadata = real.stat()
    capability = SubtitleArchiveSetCapability(
        SubtitleArchiveSetId(1),
        SubtitleReleaseId(1),
        SubtitleArchiveFormat.ZIP,
        1,
        1,
        (1,),
        metadata.st_size,
    )
    downloaded = DownloadedSubtitleArchiveSet(
        capability,
        (
            DownloadedArchiveVolume(
                SubtitleArchiveVolume(
                    1,
                    1,
                    metadata.st_size,
                    hashlib.sha256(real.read_bytes()).hexdigest(),
                ),
                link,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            ),
        ),
    )
    with pytest.raises(SubtitleArchiveError) as raised:
        asyncio.run(
            FilesystemSubtitleArchiveInspector(
                _Runner(_manifest("zip", _entry("E.ass", 1)), {"E.ass": b"x"})
            ).inspect(downloaded, season_numbers=(1,))
        )
    assert raised.value.code is SubtitleArchiveErrorCode.CONTENT_DRIFT


def test_fixed_7zz_runner_uses_only_pinned_binary_and_fixed_list_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _manifest("zip", _entry("E01.ass", 1))

    class _Process:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_data(expected)
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    async def fake_create(*args: object, **kwargs: object) -> _Process:
        assert args[0] == "/usr/bin/prlimit"
        assert "/usr/bin/7zz" in args
        assert "l" in args
        assert "-slt" in args
        assert "-mmt=1" in args
        assert args[-2] == "--"
        assert args[-1] == "/private/tmp/archive.zip"
        assert kwargs["cwd"] == "/"
        assert kwargs["env"] == {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    assert asyncio.run(
        FixedSevenZipRunner().list_manifest(
            Path("/private/tmp/archive.zip")
        )
    ) == expected


def test_fixed_7zz_runner_kills_process_when_stdout_exceeds_member_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Process:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_data(b"xx")
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.returncode: int | None = None
            self.killed = False

        async def wait(self) -> int:
            if self.returncode is None:
                await asyncio.Future()
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    processes: list[_Process] = []

    async def fake_create(*args: object, **kwargs: object) -> _Process:
        del args, kwargs
        process = _Process()
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(SubtitleArchiveError) as raised:
        asyncio.run(
            FixedSevenZipRunner().extract_member(
                Path("/private/tmp/archive.zip"),
                PurePosixPath("E01.ass"),
                max_bytes=1,
            )
        )
    assert raised.value.code is SubtitleArchiveErrorCode.LIMIT_EXCEEDED
    assert processes[0].killed is True


def test_fixed_7zz_runner_kills_process_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Stream:
        async def read(self, _size: int) -> bytes:
            await asyncio.Future()
            return b""

    class _Process:
        def __init__(self) -> None:
            self.stdout = _Stream()
            self.stderr = _Stream()
            self.returncode: int | None = None
            self.killed = False

        async def wait(self) -> int:
            if self.returncode is None:
                await asyncio.Future()
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    process = _Process()

    async def fake_create(*args: object, **kwargs: object) -> _Process:
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(
        subtitle_archive_adapter,
        "SEVENZIP_TIMEOUT_SECONDS",
        0.001,
    )

    with pytest.raises(SubtitleArchiveError) as raised:
        asyncio.run(
            FixedSevenZipRunner().list_manifest(
                Path("/private/tmp/archive.zip")
            )
        )
    assert raised.value.code is SubtitleArchiveErrorCode.UNAVAILABLE
    assert raised.value.retryable is True
    assert process.killed is True
