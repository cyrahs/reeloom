from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol, runtime_checkable

from reeloom.kernel.file_types import SUBTITLE_EXTENSIONS
from reeloom.kernel.naming import filesystem_name_key
from reeloom.kernel.subtitle_acquisition import (
    CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION,
    MAX_ARCHIVE_ENTRIES,
    MAX_ARCHIVE_VOLUME_BYTES,
    MAX_COMPRESSION_RATIO,
    MAX_SUBTITLE_MEMBER_BYTES,
    MAX_TOTAL_SUBTITLE_BYTES,
    InspectedSubtitleMember,
    PlannedSubtitleMember,
    RejectedArchiveEntry,
    RejectedArchiveEntryReason,
    SubtitleArchiveFormat,
    SubtitleArchiveSource,
)
from reeloom.ports.subtitle_acquisition import (
    DownloadedArchiveVolume,
    DownloadedSubtitleArchiveSet,
    InspectedSubtitleArchiveSet,
    SubtitleArchiveError,
    SubtitleArchiveErrorCode,
)

SEVENZIP_EXECUTABLE = "/usr/bin/7zz"
SEVENZIP_LIMIT_EXECUTABLE = "/usr/bin/prlimit"
SEVENZIP_TIMEOUT_SECONDS = 15.0
SEVENZIP_INSPECTION_TIMEOUT_SECONDS = 120.0
SEVENZIP_MANIFEST_LIMIT = 1024 * 1024
SEVENZIP_STDERR_LIMIT = 64 * 1024
SEVENZIP_CPU_SECONDS = 10
SEVENZIP_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024

_ARCHIVE_SUFFIXES = (
    ".7z",
    ".bz2",
    ".gz",
    ".iso",
    ".rar",
    ".tar",
    ".tbz2",
    ".tgz",
    ".txz",
    ".xz",
    ".zip",
)
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_SEVEN_Z_MAGIC = b"7z\xbc\xaf'\x1c"
_RAR_MAGIC = (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")


def _archive_error(
    code: SubtitleArchiveErrorCode,
    *,
    retryable: bool = False,
) -> SubtitleArchiveError:
    return SubtitleArchiveError(code, retryable=retryable)


@runtime_checkable
class SevenZipRunner(Protocol):
    async def list_manifest(self, archive_path: Path) -> bytes: ...

    async def extract_member(
        self,
        archive_path: Path,
        member_path: PurePosixPath,
        *,
        max_bytes: int,
    ) -> bytes: ...


async def _read_stream(
    reader: asyncio.StreamReader,
    *,
    limit: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await reader.read(min(64 * 1024, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ValueError("subprocess output limit exceeded")


@dataclass(frozen=True, slots=True)
class FixedSevenZipRunner:
    """Run checksum-pinned 7zz with fixed argv and bounded pipes."""

    async def list_manifest(self, archive_path: Path) -> bytes:
        return await self._run(
            (
                "l",
                "-slt",
                "-bd",
                "-bb0",
                "-bso1",
                "-bse2",
                "-bsp0",
                "-sccUTF-8",
                "-mmt=1",
                "-p__REELOOM_NO_PASSWORD__",
                "--",
                os.fspath(archive_path),
            ),
            stdout_limit=SEVENZIP_MANIFEST_LIMIT,
        )

    async def extract_member(
        self,
        archive_path: Path,
        member_path: PurePosixPath,
        *,
        max_bytes: int,
    ) -> bytes:
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_SUBTITLE_MEMBER_BYTES:
            raise _archive_error(SubtitleArchiveErrorCode.LIMIT_EXCEEDED)
        return await self._run(
            (
                "x",
                "-so",
                "-bd",
                "-bb0",
                "-bso0",
                "-bse2",
                "-bsp0",
                "-sccUTF-8",
                "-mmt=1",
                "-spd",
                "-p__REELOOM_NO_PASSWORD__",
                "--",
                os.fspath(archive_path),
                member_path.as_posix(),
            ),
            stdout_limit=max_bytes,
        )

    async def _run(
        self,
        arguments: tuple[str, ...],
        *,
        stdout_limit: int,
    ) -> bytes:
        if (
            not arguments
            or type(stdout_limit) is not int
            or stdout_limit < 1
        ):
            raise _archive_error(SubtitleArchiveErrorCode.INVALID_MANIFEST)
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                SEVENZIP_LIMIT_EXECUTABLE,
                f"--cpu={SEVENZIP_CPU_SECONDS}:{SEVENZIP_CPU_SECONDS}",
                (
                    f"--as={SEVENZIP_ADDRESS_SPACE_BYTES}:"
                    f"{SEVENZIP_ADDRESS_SPACE_BYTES}"
                ),
                "--fsize=0:0",
                "--nofile=64:64",
                "--nproc=1:1",
                "--",
                SEVENZIP_EXECUTABLE,
                *arguments,
                stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd="/",
                env={
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/usr/bin:/bin",
                },
            )
            if process.stdout is None or process.stderr is None:
                raise OSError("7zz pipes unavailable")
            async with asyncio.timeout(SEVENZIP_TIMEOUT_SECONDS):
                stdout_task = asyncio.create_task(
                    _read_stream(process.stdout, limit=stdout_limit)
                )
                stderr_task = asyncio.create_task(
                    _read_stream(process.stderr, limit=SEVENZIP_STDERR_LIMIT)
                )
                wait_task = asyncio.create_task(process.wait())
                try:
                    stdout, stderr, returncode = await asyncio.gather(
                        stdout_task,
                        stderr_task,
                        wait_task,
                    )
                except BaseException:
                    stdout_task.cancel()
                    stderr_task.cancel()
                    wait_task.cancel()
                    raise
            if returncode != 0:
                lowered = stderr.casefold()
                if b"password" in lowered or b"encrypted" in lowered:
                    raise _archive_error(SubtitleArchiveErrorCode.ENCRYPTED)
                raise _archive_error(SubtitleArchiveErrorCode.INVALID_MANIFEST)
            return stdout
        except SubtitleArchiveError:
            raise
        except ValueError:
            if process is not None and process.returncode is None:
                process.kill()
                try:
                    await process.wait()
                except Exception:
                    pass
            raise _archive_error(SubtitleArchiveErrorCode.LIMIT_EXCEEDED) from None
        except (FileNotFoundError, OSError, TimeoutError):
            if process is not None and process.returncode is None:
                process.kill()
                try:
                    await process.wait()
                except Exception:
                    pass
            raise _archive_error(
                SubtitleArchiveErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None


@dataclass(frozen=True, slots=True)
class _TechnicalManifest:
    archive_fields: tuple[tuple[str, str], ...]
    entries: tuple[tuple[tuple[str, str], ...], ...]

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "archive": list(self.archive_fields),
                    "entries": [list(item) for item in self.entries],
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()


def _field_block(lines: list[str]) -> tuple[tuple[str, str], ...]:
    fields: list[tuple[str, str]] = []
    names: set[str] = set()
    for line in lines:
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        if (
            not key
            or key in names
            or len(key.encode("utf-8")) > 64
            or len(value.encode("utf-8")) > 4_096
        ):
            raise _archive_error(SubtitleArchiveErrorCode.INVALID_MANIFEST)
        names.add(key)
        fields.append((key, value))
    return tuple(fields)


def _blocks(lines: list[str]) -> tuple[tuple[tuple[str, str], ...], ...]:
    values: list[tuple[tuple[str, str], ...]] = []
    current: list[str] = []
    for line in lines:
        if not line.strip():
            if current:
                block = _field_block(current)
                if block:
                    values.append(block)
                current = []
            continue
        current.append(line)
    if current:
        block = _field_block(current)
        if block:
            values.append(block)
    return tuple(values)


def _parse_manifest(content: bytes) -> _TechnicalManifest:
    try:
        text = content.decode("utf-8", errors="strict").replace("\r\n", "\n")
    except UnicodeDecodeError:
        raise _archive_error(SubtitleArchiveErrorCode.INVALID_MANIFEST) from None
    if "\x00" in text or len(content) > SEVENZIP_MANIFEST_LIMIT:
        raise _archive_error(SubtitleArchiveErrorCode.INVALID_MANIFEST)
    lines = text.splitlines()
    try:
        separator = lines.index("----------")
    except ValueError:
        raise _archive_error(SubtitleArchiveErrorCode.INVALID_MANIFEST) from None
    archive_blocks = _blocks(lines[:separator])
    entry_blocks = _blocks(lines[separator + 1 :])
    archive = next(
        (
            block
            for block in reversed(archive_blocks)
            if "Type" in dict(block) and "Path" in dict(block)
        ),
        None,
    )
    if archive is None or not entry_blocks or len(entry_blocks) > MAX_ARCHIVE_ENTRIES:
        raise _archive_error(SubtitleArchiveErrorCode.INVALID_MANIFEST)
    canonical_archive = tuple(
        sorted(
            (key, value)
            for key, value in archive
            if key != "Path"
        )
    )
    return _TechnicalManifest(
        canonical_archive,
        tuple(tuple(sorted(item)) for item in entry_blocks),
    )


def _integer(value: str | None) -> int:
    if value is None or not value.isdigit():
        raise _archive_error(SubtitleArchiveErrorCode.INVALID_MANIFEST)
    return int(value)


def _member_path(value: str) -> PurePosixPath:
    if (
        not value
        or len(value.encode("utf-8")) > 1_024
        or "\\" in value
        or PureWindowsPath(value).is_absolute()
        or value.startswith("/")
    ):
        raise _archive_error(SubtitleArchiveErrorCode.UNSAFE_ENTRY)
    parts = tuple(value.split("/"))
    if (
        len(parts) > 8
        or any(
            not part
            or part in {".", ".."}
            or unicodedata.normalize("NFKC", part).casefold().startswith(".env")
            or any(unicodedata.category(char).startswith("C") for char in part)
            for part in parts
        )
    ):
        raise _archive_error(SubtitleArchiveErrorCode.UNSAFE_ENTRY)
    return PurePosixPath(*parts)


def _is_special(fields: dict[str, str]) -> bool:
    if fields.get("Symbolic Link") or fields.get("Hard Link"):
        return True
    attributes = fields.get("Attributes", "").strip()
    unix_mode = attributes.split()[-1] if attributes else ""
    return bool(unix_mode) and unix_mode[0:1] in {"b", "c", "l", "p", "s"}


def _has_nested_archive(path: PurePosixPath) -> bool:
    name = path.name.casefold()
    return name.endswith(_ARCHIVE_SUFFIXES) or bool(
        re.fullmatch(r".*\.part[0-9]{1,3}\.rar", name)
        or re.fullmatch(r".*\.r[0-9]{2}", name)
    )


def _magic_matches(format: SubtitleArchiveFormat, prefix: bytes) -> bool:
    if format is SubtitleArchiveFormat.ZIP:
        return any(prefix.startswith(item) for item in _ZIP_MAGIC)
    if format is SubtitleArchiveFormat.SEVEN_Z:
        return prefix.startswith(_SEVEN_Z_MAGIC)
    return any(prefix.startswith(item) for item in _RAR_MAGIC)


def _volume_content(volume: DownloadedArchiveVolume) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise _archive_error(SubtitleArchiveErrorCode.UNAVAILABLE)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            volume.path,
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != volume.volume.size_bytes
            or metadata.st_dev != volume.device
            or metadata.st_ino != volume.inode
            or metadata.st_mtime_ns != volume.mtime_ns
            or metadata.st_ctime_ns != volume.ctime_ns
            or metadata.st_size > MAX_ARCHIVE_VOLUME_BYTES
        ):
            raise _archive_error(SubtitleArchiveErrorCode.CONTENT_DRIFT)
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise _archive_error(SubtitleArchiveErrorCode.CONTENT_DRIFT)
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if hashlib.sha256(content).hexdigest() != volume.volume.sha256:
            raise _archive_error(SubtitleArchiveErrorCode.CONTENT_DRIFT)
        return content
    except SubtitleArchiveError:
        raise
    except OSError:
        raise _archive_error(SubtitleArchiveErrorCode.CONTENT_DRIFT) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class FilesystemSubtitleArchiveInspector:
    runner: SevenZipRunner = FixedSevenZipRunner()

    @property
    def inspector_version(self) -> str:
        return CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION

    async def inspect(
        self,
        downloaded: DownloadedSubtitleArchiveSet,
        *,
        season_numbers: tuple[int, ...],
    ) -> InspectedSubtitleArchiveSet:
        if (
            not isinstance(downloaded, DownloadedSubtitleArchiveSet)
            or not isinstance(self.runner, SevenZipRunner)
            or not isinstance(season_numbers, tuple)
            or not season_numbers
            or tuple(sorted(set(season_numbers))) != season_numbers
            or any(type(item) is not int or not 0 <= item <= 999 for item in season_numbers)
        ):
            raise _archive_error(SubtitleArchiveErrorCode.CAPABILITY_CHANGED)
        try:
            async with asyncio.timeout(SEVENZIP_INSPECTION_TIMEOUT_SECONDS):
                return await self._inspect(downloaded, season_numbers=season_numbers)
        except SubtitleArchiveError:
            raise
        except TimeoutError:
            raise _archive_error(
                SubtitleArchiveErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None

    async def _inspect(
        self,
        downloaded: DownloadedSubtitleArchiveSet,
        *,
        season_numbers: tuple[int, ...],
    ) -> InspectedSubtitleArchiveSet:
            prefixes = tuple(_volume_content(item)[:16] for item in downloaded.volumes)
            if any(
                not _magic_matches(downloaded.capability.format, prefix)
                for prefix in prefixes
            ):
                raise _archive_error(SubtitleArchiveErrorCode.INVALID_FORMAT)
            manifest = _parse_manifest(
                await self.runner.list_manifest(downloaded.volumes[0].path)
            )
            archive_fields = dict(manifest.archive_fields)
            expected_type = {
                SubtitleArchiveFormat.ZIP: "zip",
                SubtitleArchiveFormat.SEVEN_Z: "7z",
                SubtitleArchiveFormat.RAR: "rar",
            }[downloaded.capability.format]
            if archive_fields.get("Type", "").casefold() != expected_type:
                raise _archive_error(SubtitleArchiveErrorCode.INVALID_FORMAT)
            if len(downloaded.volumes) > 1:
                if (
                    downloaded.capability.format is not SubtitleArchiveFormat.RAR
                    or _integer(archive_fields.get("Volumes"))
                    != len(downloaded.volumes)
                ):
                    raise _archive_error(SubtitleArchiveErrorCode.INVALID_FORMAT)

            entries: list[tuple[PurePosixPath, dict[str, str], int]] = []
            name_keys: set[str] = set()
            total_expanded = 0
            for raw_entry in manifest.entries:
                fields = dict(raw_entry)
                path = _member_path(fields.get("Path", ""))
                key = filesystem_name_key(path.as_posix())
                if key in name_keys:
                    raise _archive_error(SubtitleArchiveErrorCode.UNSAFE_ENTRY)
                name_keys.add(key)
                is_directory = fields.get("Folder") == "+"
                if fields.get("Encrypted") != "-":
                    raise _archive_error(SubtitleArchiveErrorCode.ENCRYPTED)
                if _is_special(fields):
                    raise _archive_error(SubtitleArchiveErrorCode.SPECIAL_FILE)
                size = _integer(fields.get("Size"))
                if is_directory:
                    if size != 0:
                        raise _archive_error(SubtitleArchiveErrorCode.INVALID_MANIFEST)
                    continue
                total_expanded += size
                if total_expanded > MAX_TOTAL_SUBTITLE_BYTES:
                    raise _archive_error(SubtitleArchiveErrorCode.LIMIT_EXCEEDED)
                if _has_nested_archive(path):
                    raise _archive_error(SubtitleArchiveErrorCode.NESTED_ARCHIVE)
                entries.append((path, fields, size))
            archive_bytes = sum(item.volume.size_bytes for item in downloaded.volumes)
            if total_expanded > archive_bytes * MAX_COMPRESSION_RATIO:
                raise _archive_error(SubtitleArchiveErrorCode.LIMIT_EXCEEDED)

            members: list[InspectedSubtitleMember] = []
            rejected: list[RejectedArchiveEntry] = []
            for path, _fields, size in entries:
                if path.suffix.casefold() not in SUBTITLE_EXTENSIONS:
                    rejected.append(
                        RejectedArchiveEntry(
                            downloaded.capability.archive_set_id,
                            hashlib.sha256(path.as_posix().encode("utf-8")).hexdigest(),
                            RejectedArchiveEntryReason.UNSUPPORTED_TYPE,
                        )
                    )
                    continue
                if not 1 <= size <= MAX_SUBTITLE_MEMBER_BYTES:
                    raise _archive_error(SubtitleArchiveErrorCode.LIMIT_EXCEEDED)
                content = await self.runner.extract_member(
                    downloaded.volumes[0].path,
                    path,
                    max_bytes=size,
                )
                if len(content) != size:
                    raise _archive_error(SubtitleArchiveErrorCode.CONTENT_DRIFT)
                members.append(
                    InspectedSubtitleMember(
                        downloaded.capability.archive_set_id,
                        path,
                        size,
                        hashlib.sha256(content).hexdigest(),
                    )
                )
            if not members:
                raise _archive_error(SubtitleArchiveErrorCode.INVALID_MANIFEST)
            for item in downloaded.volumes:
                _volume_content(item)
            source = SubtitleArchiveSource(
                release_id=downloaded.capability.release_id,
                archive_set_id=downloaded.capability.archive_set_id,
                format=downloaded.capability.format,
                season_numbers=season_numbers,
                thread_id=downloaded.capability.thread_id,
                post_id=downloaded.capability.post_id,
                manifest_digest=manifest.digest,
                volumes=tuple(item.volume for item in downloaded.volumes),
            )
            return InspectedSubtitleArchiveSet(
                source,
                tuple(members),
                tuple(rejected),
            )

    async def extract_member(
        self,
        downloaded: DownloadedSubtitleArchiveSet,
        member: PlannedSubtitleMember,
    ) -> bytes:
        if (
            not isinstance(downloaded, DownloadedSubtitleArchiveSet)
            or not isinstance(member, PlannedSubtitleMember)
            or member.archive_set_id
            != downloaded.capability.archive_set_id
        ):
            raise _archive_error(SubtitleArchiveErrorCode.CAPABILITY_CHANGED)
        try:
            async with asyncio.timeout(SEVENZIP_INSPECTION_TIMEOUT_SECONDS):
                for volume in downloaded.volumes:
                    _volume_content(volume)
                content = await self.runner.extract_member(
                    downloaded.volumes[0].path,
                    member.source_path,
                    max_bytes=member.size_bytes,
                )
                if (
                    len(content) != member.size_bytes
                    or hashlib.sha256(content).hexdigest() != member.sha256
                ):
                    raise _archive_error(SubtitleArchiveErrorCode.CONTENT_DRIFT)
                for volume in downloaded.volumes:
                    _volume_content(volume)
                return content
        except SubtitleArchiveError:
            raise
        except TimeoutError:
            raise _archive_error(
                SubtitleArchiveErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None
