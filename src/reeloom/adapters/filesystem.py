from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.file_types import candidate_kind_for_filename
from reeloom.kernel.mapping import MappingDraft
from reeloom.kernel.movie import MovieMappingDraft, compile_movie_plan_draft
from reeloom.kernel.movie_plan import MovieRenamePlan
from reeloom.kernel.naming import (
    MovieIdentity,
    SeriesIdentity,
    SubtitleVariant,
    filesystem_name_key,
)
from reeloom.kernel.rename_plan import (
    RenamePlan,
    RootBinding,
    compile_plan_draft,
)
from reeloom.kernel.scanner import (
    CandidateRecord,
    ScannedCandidateSnapshot,
    ScannedFile,
    build_candidate_snapshot,
)
from reeloom.kernel.subtitle_acquisition import (
    MAX_EMBEDDED_SUBTITLE_TRACKS,
    EmbeddedChineseStatus,
    EmbeddedSubtitleCodec,
    EmbeddedSubtitleInspection,
    EmbeddedSubtitleLanguage,
    EmbeddedSubtitleProbeStatus,
    EmbeddedSubtitleTrack,
    EmbeddedSubtitleTrackId,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import (
    AuthorizedRoot,
    is_forbidden_env_name,
)
from reeloom.ports.subtitles import SubtitleSample

FFPROBE_EXECUTABLE = "/usr/bin/ffprobe"
FFPROBE_LIMIT_EXECUTABLE = "/usr/bin/prlimit"
FFPROBE_TIMEOUT_SECONDS = 10.0
FFPROBE_STDOUT_LIMIT = 64 * 1024
FFPROBE_STDERR_LIMIT = 16 * 1024
FFPROBE_PROBE_BYTES = 16 * 1024 * 1024
FFPROBE_ANALYZE_MICROSECONDS = 10 * 1_000_000
FFPROBE_CPU_SECONDS = 5
FFPROBE_ADDRESS_SPACE_BYTES = 256 * 1024 * 1024

_TRUSTED_ZERO_STREAM_EXTENSIONS = frozenset(
    {".avi", ".m4v", ".mkv", ".mp4", ".webm"}
)
_FFPROBE_TOP_LEVEL_KEYS = frozenset(
    {"programs", "stream_groups", "streams"}
)
_FFPROBE_WRAPPER_KEYS = frozenset({"programs", "stream_groups"})


class FfprobeResultStatus(StrEnum):
    COMPLETE = "complete"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class FfprobeProcessResult:
    status: FfprobeResultStatus
    stdout: bytes = b""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status, FfprobeResultStatus)
            or not isinstance(self.stdout, bytes)
            or len(self.stdout) > FFPROBE_STDOUT_LIMIT
            or (
                self.status is FfprobeResultStatus.INDETERMINATE
                and self.stdout
            )
        ):
            raise ValueError("invalid ffprobe process result")


@runtime_checkable
class FfprobeRunner(Protocol):
    async def probe(self, file_descriptor: int) -> FfprobeProcessResult: ...


async def _read_process_stream(
    reader: asyncio.StreamReader,
    *,
    limit: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await reader.read(min(4096, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ValueError("subprocess output limit exceeded")


@dataclass(frozen=True, slots=True)
class FixedFfprobeRunner:
    """Run one fixed ffprobe command against an inherited read-only FD."""

    async def probe(self, file_descriptor: int) -> FfprobeProcessResult:
        if type(file_descriptor) is not int or file_descriptor < 0:
            return FfprobeProcessResult(
                FfprobeResultStatus.INDETERMINATE
            )
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                FFPROBE_LIMIT_EXECUTABLE,
                f"--cpu={FFPROBE_CPU_SECONDS}:{FFPROBE_CPU_SECONDS}",
                (
                    f"--as={FFPROBE_ADDRESS_SPACE_BYTES}:"
                    f"{FFPROBE_ADDRESS_SPACE_BYTES}"
                ),
                "--fsize=0:0",
                "--nofile=64:64",
                "--",
                FFPROBE_EXECUTABLE,
                "-v",
                "error",
                "-protocol_whitelist",
                "fd",
                "-probesize",
                str(FFPROBE_PROBE_BYTES),
                "-analyzeduration",
                str(FFPROBE_ANALYZE_MICROSECONDS),
                "-select_streams",
                "s",
                "-show_entries",
                (
                    "stream=index,codec_name:stream_tags=language:"
                    "stream_disposition=default,forced"
                ),
                "-of",
                "json",
                "-fd",
                str(file_descriptor),
                "fd:",
                stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd="/",
                env={
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin",
                },
                pass_fds=(file_descriptor,),
                start_new_session=True,
            )
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("ffprobe pipes unavailable")
            stdout, _stderr, return_code = await asyncio.wait_for(
                asyncio.gather(
                    _read_process_stream(
                        process.stdout,
                        limit=FFPROBE_STDOUT_LIMIT,
                    ),
                    _read_process_stream(
                        process.stderr,
                        limit=FFPROBE_STDERR_LIMIT,
                    ),
                    process.wait(),
                ),
                timeout=FFPROBE_TIMEOUT_SECONDS,
            )
            if return_code != 0:
                raise RuntimeError("ffprobe failed")
            return FfprobeProcessResult(
                FfprobeResultStatus.COMPLETE,
                stdout,
            )
        except (
            asyncio.TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
        ):
            if process is not None and process.returncode is None:
                process.kill()
                try:
                    await process.wait()
                except (OSError, ProcessLookupError):
                    pass
            return FfprobeProcessResult(
                FfprobeResultStatus.INDETERMINATE
            )


def _codec(value: object) -> EmbeddedSubtitleCodec:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 64:
        return EmbeddedSubtitleCodec.UNKNOWN
    return {
        "ass": EmbeddedSubtitleCodec.ASS,
        "ssa": EmbeddedSubtitleCodec.ASS,
        "subrip": EmbeddedSubtitleCodec.SUBRIP,
        "srt": EmbeddedSubtitleCodec.SUBRIP,
        "hdmv_pgs_subtitle": EmbeddedSubtitleCodec.PGS,
        "webvtt": EmbeddedSubtitleCodec.WEBVTT,
        "dvb_subtitle": EmbeddedSubtitleCodec.DVB,
        "mov_text": EmbeddedSubtitleCodec.MOV_TEXT,
    }.get(value.casefold(), EmbeddedSubtitleCodec.OTHER)


def _language(value: object) -> EmbeddedSubtitleLanguage:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 32:
        return EmbeddedSubtitleLanguage.UNKNOWN
    normalized = value.strip().casefold().replace("_", "-")
    if normalized in {"zh-hans", "zh-cn", "chs", "sc"}:
        return EmbeddedSubtitleLanguage.ZH_HANS
    if normalized in {"zh-hant", "zh-tw", "cht", "tc"}:
        return EmbeddedSubtitleLanguage.ZH_HANT
    if normalized in {"zh", "zho", "chi"}:
        return EmbeddedSubtitleLanguage.ZH
    if normalized in {"ja", "jpn"}:
        return EmbeddedSubtitleLanguage.JA
    if normalized in {"en", "eng"}:
        return EmbeddedSubtitleLanguage.EN
    if normalized in {"", "und"}:
        return EmbeddedSubtitleLanguage.UNKNOWN
    return EmbeddedSubtitleLanguage.OTHER


def _flag(value: object) -> bool | None:
    if value in (0, False):
        return False
    if value in (1, True):
        return True
    return None


def _parse_ffprobe_tracks(
    content: bytes,
) -> tuple[EmbeddedSubtitleTrack, ...] | None:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or "streams" not in payload
        or not set(payload) <= _FFPROBE_TOP_LEVEL_KEYS
        or any(
            key in payload and not isinstance(payload[key], list)
            for key in _FFPROBE_WRAPPER_KEYS
        )
    ):
        return None
    streams = payload["streams"]
    if (
        not isinstance(streams, list)
        or len(streams) > MAX_EMBEDDED_SUBTITLE_TRACKS
    ):
        return None
    parsed: list[
        tuple[int, EmbeddedSubtitleCodec, EmbeddedSubtitleLanguage, bool, bool]
    ] = []
    for stream in streams:
        if not isinstance(stream, dict):
            return None
        index = stream.get("index")
        if type(index) is not int or index < 0:
            return None
        tags = stream.get("tags", {})
        disposition = stream.get("disposition", {})
        if not isinstance(tags, dict) or not isinstance(disposition, dict):
            return None
        default = _flag(disposition.get("default", 0))
        forced = _flag(disposition.get("forced", 0))
        if default is None or forced is None:
            return None
        parsed.append(
            (
                index,
                _codec(stream.get("codec_name")),
                _language(tags.get("language")),
                default,
                forced,
            )
        )
    if len({item[0] for item in parsed}) != len(parsed):
        return None
    return tuple(
        EmbeddedSubtitleTrack(
            track_id=EmbeddedSubtitleTrackId(ordinal),
            codec=codec,
            language=language,
            default=default,
            forced=forced,
        )
        for ordinal, (_, codec, language, default, forced) in enumerate(
            sorted(parsed),
            start=1,
        )
    )


def _read_prefix(file_descriptor: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes
    while remaining:
        chunk = os.read(file_descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class ScanLimits:
    max_candidates: int = 10_000
    max_entries: int = 50_000
    max_depth: int = 32

    def __post_init__(self) -> None:
        if (
            type(self.max_candidates) is not int
            or self.max_candidates < 1
            or type(self.max_entries) is not int
            or self.max_entries < 1
            or type(self.max_depth) is not int
            or self.max_depth < 1
        ):
            raise DomainError(ErrorCode.SCAN_LIMIT_EXCEEDED)


@dataclass(slots=True)
class _ScanProgress:
    entries_seen: int = 0


@dataclass(frozen=True, slots=True)
class FilesystemScanResult:
    authorized_root: AuthorizedRoot
    snapshot: ScannedCandidateSnapshot


@dataclass(frozen=True, slots=True)
class FilesystemPlanCompiler:
    """Compile one read-only plan for a fixed scan and output root."""

    scan: FilesystemScanResult
    output_root: AuthorizedRoot

    @property
    def snapshot_id(self) -> str:
        return self.scan.snapshot.snapshot_id

    @property
    def candidate_count(self) -> int:
        return len(self.scan.snapshot.records)

    @property
    def source_root_binding(self) -> RootBinding:
        return self._root_binding(self.scan.authorized_root)

    @property
    def output_root_binding(self) -> RootBinding:
        return self._root_binding(self.output_root)

    def compile(
        self,
        *,
        run_id: str,
        work_type: TmdbWorkType,
        series: SeriesIdentity,
        mapping: MappingDraft,
        subtitle_variants: tuple[
            tuple[CandidateId, SubtitleVariant],
            ...,
        ],
        created_at: datetime,
    ) -> RenamePlan:
        draft = compile_plan_draft(
            series=series,
            mapping=mapping,
            candidates=self.scan.snapshot,
            subtitle_variants=subtitle_variants,
        )
        checked = self._check_destinations(
            tuple(move.destination for move in draft.moves)
        )
        return RenamePlan.create(
            run_id=run_id,
            work_type=work_type,
            created_at=created_at,
            source_root=self.source_root_binding,
            output_root=self.output_root_binding,
            candidate_snapshot=self.scan.snapshot,
            subtitle_variants=subtitle_variants,
            draft=draft,
            checked_destinations=checked,
        )

    def compile_movie(
        self,
        *,
        run_id: str,
        movie: MovieIdentity,
        mapping: MovieMappingDraft,
        subtitle_variants: tuple[
            tuple[CandidateId, SubtitleVariant],
            ...,
        ],
        created_at: datetime,
    ) -> MovieRenamePlan:
        draft = compile_movie_plan_draft(
            movie=movie,
            mapping=mapping,
            candidates=self.scan.snapshot,
            subtitle_variants=subtitle_variants,
        )
        destinations = tuple(move.destination for move in draft.moves)
        self._check_movie_root_absent(destinations)
        return MovieRenamePlan.create(
            run_id=run_id,
            created_at=created_at,
            source_root=self.source_root_binding,
            output_root=self.output_root_binding,
            candidate_snapshot=self.scan.snapshot,
            subtitle_variants=subtitle_variants,
            draft=draft,
            checked_destinations=destinations,
        )

    @staticmethod
    def _root_binding(root: AuthorizedRoot) -> RootBinding:
        return RootBinding(
            path=PurePosixPath(root.path.as_posix()),
            device=root.device,
            inode=root.inode,
        )

    def _check_destinations(
        self,
        destinations: tuple[PurePosixPath, ...],
    ) -> tuple[PurePosixPath, ...]:
        root_fd = FilesystemScanner._open_root(self.output_root)
        try:
            for destination in destinations:
                self._check_destination(root_fd, destination)
        finally:
            os.close(root_fd)
        return destinations

    def _check_movie_root_absent(
        self,
        destinations: tuple[PurePosixPath, ...],
    ) -> None:
        if (
            not destinations
            or len({item.parts[0] for item in destinations}) != 1
        ):
            raise DomainError(ErrorCode.PLAN_PREFLIGHT_MISMATCH)
        root_fd = FilesystemScanner._open_root(self.output_root)
        try:
            try:
                entries = os.listdir(root_fd)
            except OSError:
                raise DomainError(ErrorCode.SCAN_FAILED) from None
            target = filesystem_name_key(destinations[0].parts[0])
            for entry in entries:
                if filesystem_name_key(entry) != target:
                    continue
                try:
                    metadata = os.stat(
                        entry,
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    raise DomainError(ErrorCode.SCAN_FAILED) from None
                if stat.S_ISLNK(metadata.st_mode):
                    raise DomainError(ErrorCode.SYMLINK_NOT_ALLOWED)
                raise DomainError(ErrorCode.DESTINATION_COLLISION)
        finally:
            os.close(root_fd)

    @staticmethod
    def _check_destination(
        root_fd: int,
        destination: PurePosixPath,
    ) -> None:
        current_fd = root_fd
        try:
            for part in destination.parts[:-1]:
                try:
                    metadata = os.stat(
                        part,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return
                except OSError:
                    raise DomainError(ErrorCode.SCAN_FAILED) from None
                if stat.S_ISLNK(metadata.st_mode):
                    raise DomainError(ErrorCode.SYMLINK_NOT_ALLOWED)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise DomainError(ErrorCode.DESTINATION_COLLISION)
                next_fd = FilesystemScanner._open_directory(
                    part,
                    parent_fd=current_fd,
                )
                try:
                    opened = os.fstat(next_fd)
                except OSError:
                    try:
                        os.close(next_fd)
                    except OSError:
                        pass
                    raise DomainError(ErrorCode.SCAN_FAILED) from None
                if (
                    opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                ):
                    os.close(next_fd)
                    raise DomainError(ErrorCode.SCAN_FAILED)
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd

            try:
                metadata = os.stat(
                    destination.name,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            except OSError:
                raise DomainError(ErrorCode.SCAN_FAILED) from None
            if stat.S_ISLNK(metadata.st_mode):
                raise DomainError(ErrorCode.SYMLINK_NOT_ALLOWED)
            raise DomainError(ErrorCode.DESTINATION_COLLISION)
        finally:
            if current_fd != root_fd:
                os.close(current_fd)


@dataclass(frozen=True, slots=True)
class FilesystemSubtitleSampleProvider:
    """Read only a bounded prefix for a subtitle already in one scan."""

    scan: FilesystemScanResult

    @property
    def snapshot_id(self) -> str:
        return self.scan.snapshot.snapshot_id

    @property
    def candidate_count(self) -> int:
        return len(self.scan.snapshot.records)

    async def sample(
        self,
        subtitle_id: CandidateId,
        *,
        max_bytes: int,
    ) -> SubtitleSample:
        if (
            not isinstance(subtitle_id, CandidateId)
            or subtitle_id.kind is not CandidateKind.SUBTITLE
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= 64 * 1024
        ):
            raise DomainError(ErrorCode.INVALID_SUBTITLE_VARIANT)
        record = self.scan.snapshot.record_for(subtitle_id)
        if any(
            value is None
            for value in (
                record.device,
                record.inode,
                record.mtime_ns,
                record.ctime_ns,
                record.sample_digest,
            )
        ):
            raise DomainError(ErrorCode.SCAN_FAILED)
        root_fd = FilesystemScanner._open_root(self.scan.authorized_root)
        current_fd = root_fd
        try:
            for part in record.relative_path.parts[:-1]:
                next_fd = FilesystemScanner._open_directory(
                    part,
                    parent_fd=current_fd,
                )
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd

            no_follow = getattr(os, "O_NOFOLLOW", None)
            if no_follow is None:
                raise DomainError(ErrorCode.SCAN_FAILED)
            flags = (
                os.O_RDONLY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            file_fd: int | None = None
            try:
                file_fd = os.open(
                    record.relative_path.name,
                    flags,
                    dir_fd=current_fd,
                )
                metadata = os.fstat(file_fd)
                if not self._matches_record(metadata, record):
                    raise DomainError(ErrorCode.SCAN_FAILED)
                content = _read_prefix(file_fd, 64 * 1024)
                if not self._matches_record(os.fstat(file_fd), record):
                    raise DomainError(ErrorCode.SCAN_FAILED)
                if (
                    hashlib.sha256(content).hexdigest()
                    != record.sample_digest
                ):
                    raise DomainError(ErrorCode.SCAN_FAILED)
            except OSError:
                raise DomainError(ErrorCode.SCAN_FAILED) from None
            finally:
                if file_fd is not None:
                    os.close(file_fd)
        finally:
            if current_fd != root_fd:
                os.close(current_fd)
            os.close(root_fd)

        return SubtitleSample(
            display_name=record.candidate.display_name,
            content=content[:max_bytes],
        )

    @staticmethod
    def _matches_record(
        metadata: os.stat_result,
        record: CandidateRecord,
    ) -> bool:
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_size == record.size_bytes
            and metadata.st_dev == record.device
            and metadata.st_ino == record.inode
            and metadata.st_mtime_ns == record.mtime_ns
            and metadata.st_ctime_ns == record.ctime_ns
        )


@dataclass(frozen=True, slots=True)
class FilesystemVideoSubtitleInspector:
    """Inspect one snapshot-bound video through a fixed ffprobe runner."""

    scan: FilesystemScanResult
    runner: FfprobeRunner = FixedFfprobeRunner()

    @property
    def snapshot_id(self) -> str:
        return self.scan.snapshot.snapshot_id

    @property
    def candidate_count(self) -> int:
        return len(self.scan.snapshot.records)

    async def inspect(
        self,
        video_id: CandidateId,
        *,
        season_number: int,
    ) -> EmbeddedSubtitleInspection:
        if (
            not isinstance(video_id, CandidateId)
            or video_id.kind is not CandidateKind.VIDEO
            or type(season_number) is not int
            or not 0 <= season_number <= 999
            or not isinstance(self.runner, FfprobeRunner)
        ):
            raise DomainError(ErrorCode.INVALID_EMBEDDED_SUBTITLE_DATA)
        record = self.scan.snapshot.record_for(video_id)
        if any(
            value is None
            for value in (
                record.device,
                record.inode,
                record.mtime_ns,
                record.ctime_ns,
            )
        ):
            raise DomainError(ErrorCode.SCAN_FAILED)
        root_fd = FilesystemScanner._open_root(self.scan.authorized_root)
        current_fd = root_fd
        try:
            for part in record.relative_path.parts[:-1]:
                next_fd = FilesystemScanner._open_directory(
                    part,
                    parent_fd=current_fd,
                )
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd

            no_follow = getattr(os, "O_NOFOLLOW", None)
            if no_follow is None:
                raise DomainError(ErrorCode.SCAN_FAILED)
            file_fd: int | None = None
            try:
                file_fd = os.open(
                    record.relative_path.name,
                    os.O_RDONLY
                    | no_follow
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=current_fd,
                )
                before = os.fstat(file_fd)
                if not FilesystemSubtitleSampleProvider._matches_record(
                    before,
                    record,
                ):
                    raise DomainError(ErrorCode.SCAN_FAILED)
                result = await self.runner.probe(file_fd)
                if not isinstance(result, FfprobeProcessResult):
                    raise DomainError(
                        ErrorCode.INVALID_EMBEDDED_SUBTITLE_DATA
                    )
                after = os.fstat(file_fd)
                if not FilesystemSubtitleSampleProvider._matches_record(
                    after,
                    record,
                ):
                    raise DomainError(ErrorCode.SCAN_FAILED)
            except OSError:
                raise DomainError(ErrorCode.SCAN_FAILED) from None
            finally:
                if file_fd is not None:
                    os.close(file_fd)
        finally:
            if current_fd != root_fd:
                os.close(current_fd)
            os.close(root_fd)

        if result.status is FfprobeResultStatus.INDETERMINATE:
            return EmbeddedSubtitleInspection(
                video_id=video_id,
                season_number=season_number,
                probe_status=EmbeddedSubtitleProbeStatus.INDETERMINATE,
                chinese_status=EmbeddedChineseStatus.UNKNOWN,
                tracks=(),
            )
        tracks = _parse_ffprobe_tracks(result.stdout)
        if tracks is None:
            return EmbeddedSubtitleInspection(
                video_id=video_id,
                season_number=season_number,
                probe_status=EmbeddedSubtitleProbeStatus.INDETERMINATE,
                chinese_status=EmbeddedChineseStatus.UNKNOWN,
                tracks=(),
            )
        if tracks:
            chinese_status = (
                EmbeddedChineseStatus.PRESENT
                if any(item.language.is_chinese for item in tracks)
                else (
                    EmbeddedChineseStatus.UNKNOWN
                    if any(
                        item.language is EmbeddedSubtitleLanguage.UNKNOWN
                        for item in tracks
                    )
                    else EmbeddedChineseStatus.ABSENT
                )
            )
            return EmbeddedSubtitleInspection(
                video_id=video_id,
                season_number=season_number,
                probe_status=EmbeddedSubtitleProbeStatus.PRESENT,
                chinese_status=chinese_status,
                tracks=tracks,
            )
        if record.relative_path.suffix.casefold() not in (
            _TRUSTED_ZERO_STREAM_EXTENSIONS
        ):
            return EmbeddedSubtitleInspection(
                video_id=video_id,
                season_number=season_number,
                probe_status=EmbeddedSubtitleProbeStatus.INDETERMINATE,
                chinese_status=EmbeddedChineseStatus.UNKNOWN,
                tracks=(),
            )
        return EmbeddedSubtitleInspection(
            video_id=video_id,
            season_number=season_number,
            probe_status=EmbeddedSubtitleProbeStatus.ABSENT,
            chinese_status=EmbeddedChineseStatus.ABSENT,
            tracks=(),
        )


@dataclass(frozen=True, slots=True)
class FilesystemScanner:
    limits: ScanLimits = ScanLimits()

    def scan(self, root: AuthorizedRoot) -> FilesystemScanResult:
        files: list[ScannedFile] = []
        directory_fd = self._open_root(root)
        try:
            self._scan_directory(
                directory_fd=directory_fd,
                relative_directory=None,
                depth=0,
                files=files,
                progress=_ScanProgress(),
            )
        finally:
            os.close(directory_fd)
        return FilesystemScanResult(
            authorized_root=root,
            snapshot=build_candidate_snapshot(files),
        )

    @classmethod
    def _open_root(cls, root: AuthorizedRoot) -> int:
        current_fd = cls._open_directory(Path(root.path.anchor))
        try:
            for part in root.path.parts[1:]:
                next_fd = cls._open_directory(
                    part,
                    parent_fd=current_fd,
                )
                os.close(current_fd)
                current_fd = next_fd
            root_stat = os.fstat(current_fd)
            if (
                root_stat.st_dev != root.device
                or root_stat.st_ino != root.inode
            ):
                raise DomainError(ErrorCode.SCAN_FAILED)
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    @staticmethod
    def _open_directory(
        path: Path | str,
        *,
        parent_fd: int | None = None,
    ) -> int:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory is None:
            raise DomainError(ErrorCode.SCAN_FAILED)
        flags = os.O_RDONLY | no_follow | directory
        try:
            return os.open(path, flags, dir_fd=parent_fd)
        except OSError as error:
            raise DomainError(ErrorCode.SCAN_FAILED) from error

    def _scan_directory(
        self,
        *,
        directory_fd: int,
        relative_directory: PurePosixPath | None,
        depth: int,
        files: list[ScannedFile],
        progress: _ScanProgress,
    ) -> None:
        if depth > self.limits.max_depth:
            raise DomainError(ErrorCode.SCAN_LIMIT_EXCEEDED)

        try:
            with os.scandir(directory_fd) as entries:
                ordered_entries = []
                for entry in entries:
                    progress.entries_seen += 1
                    if progress.entries_seen > self.limits.max_entries:
                        raise DomainError(ErrorCode.SCAN_LIMIT_EXCEEDED)
                    ordered_entries.append(entry)
                ordered_entries.sort(key=lambda entry: entry.name)
        except OSError as error:
            raise DomainError(ErrorCode.SCAN_FAILED) from error

        for entry in ordered_entries:
            if is_forbidden_env_name(entry.name):
                continue
            try:
                if entry.is_symlink():
                    continue
                relative_path = (
                    PurePosixPath(entry.name)
                    if relative_directory is None
                    else relative_directory / entry.name
                )
                if entry.is_dir(follow_symlinks=False):
                    child_fd = self._open_directory(
                        entry.name,
                        parent_fd=directory_fd,
                    )
                    try:
                        self._scan_directory(
                            directory_fd=child_fd,
                            relative_directory=relative_path,
                            depth=depth + 1,
                            files=files,
                            progress=progress,
                        )
                    finally:
                        os.close(child_fd)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                kind = candidate_kind_for_filename(entry.name)
                if kind is None:
                    continue
                file_stat = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
            except OSError as error:
                raise DomainError(ErrorCode.SCAN_FAILED) from error

            files.append(
                ScannedFile(
                    relative_path=relative_path,
                    kind=kind,
                    size_bytes=file_stat.st_size,
                    device=file_stat.st_dev,
                    inode=file_stat.st_ino,
                    mtime_ns=file_stat.st_mtime_ns,
                    ctime_ns=file_stat.st_ctime_ns,
                    sample_digest=(
                        self._subtitle_sample_digest(
                            directory_fd=directory_fd,
                            name=entry.name,
                            expected=file_stat,
                        )
                        if kind is CandidateKind.SUBTITLE
                        else None
                    ),
                )
            )
            if len(files) > self.limits.max_candidates:
                raise DomainError(ErrorCode.SCAN_LIMIT_EXCEEDED)

    @staticmethod
    def _subtitle_sample_digest(
        *,
        directory_fd: int,
        name: str,
        expected: os.stat_result,
    ) -> str:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise DomainError(ErrorCode.SCAN_FAILED)
        flags = (
            os.O_RDONLY
            | no_follow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        file_fd: int | None = None
        try:
            file_fd = os.open(name, flags, dir_fd=directory_fd)
            before = os.fstat(file_fd)
            if not FilesystemScanner._same_identity(before, expected):
                raise DomainError(ErrorCode.SCAN_FAILED)
            content = _read_prefix(file_fd, 64 * 1024)
            if not FilesystemScanner._same_identity(
                os.fstat(file_fd),
                expected,
            ):
                raise DomainError(ErrorCode.SCAN_FAILED)
            return hashlib.sha256(content).hexdigest()
        except OSError:
            raise DomainError(ErrorCode.SCAN_FAILED) from None
        finally:
            if file_fd is not None:
                os.close(file_fd)

    @staticmethod
    def _same_identity(
        first: os.stat_result,
        second: os.stat_result,
    ) -> bool:
        return (
            stat.S_ISREG(first.st_mode)
            and stat.S_ISREG(second.st_mode)
            and first.st_dev == second.st_dev
            and first.st_ino == second.st_ino
            and first.st_size == second.st_size
            and first.st_mtime_ns == second.st_mtime_ns
            and first.st_ctime_ns == second.st_ctime_ns
        )
