from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.file_types import SUBTITLE_EXTENSIONS
from reeloom.kernel.naming import filesystem_name_key
from reeloom.kernel.rename_plan import RootBinding
from reeloom.kernel.schema import check_fields
from reeloom.kernel.semantic_identity import (
    SemanticCandidateSnapshot,
    SemanticRootBinding,
)
from reeloom.kernel.tmdb import TmdbWorkType

CURRENT_SUBTITLE_ACQUISITION_SCHEMA_VERSION = (
    "subtitle-acquisition-plan-v1"
)
CURRENT_SUBTITLE_ACQUISITION_SCHEMA_VERSION_V2 = (
    "subtitle-acquisition-plan-v2"
)
CURRENT_SUBTITLE_ACQUISITION_POLICY_VERSION = "m13-acquisition-v1"
CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION = "acgrip-discuz-v3"
CURRENT_SUBTITLE_SEARCH_PARSER_VERSION = "acgrip-html-v2"
CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION = "7zz-26.02-v1"

MAX_EMBEDDED_SUBTITLE_TRACKS = 32
MAX_SUBTITLE_SEARCH_ALIASES = 3
MAX_SUBTITLE_SEARCH_ALIAS_BYTES = 240
MAX_SEARCH_RESULTS_PER_PAGE = 10
MAX_SEARCH_RESULTS_PER_RUN = 50
MAX_ARCHIVE_SETS = 12
MAX_ARCHIVE_VOLUMES = 8
MAX_ARCHIVE_VOLUME_BYTES = 16 * 1024 * 1024
MAX_TOTAL_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 256
MAX_SUBTITLE_MEMBER_BYTES = 32 * 1024 * 1024
MAX_TOTAL_SUBTITLE_BYTES = 128 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100

_MAX_CANONICAL_BYTES = 2 * 1024 * 1024
_MAX_RUN_ID_BYTES = 128
_MAX_TEXT_BYTES = MAX_SUBTITLE_SEARCH_ALIAS_BYTES
_MAX_EXCERPT_BYTES = 512
_MAX_HINT_BYTES = 160
_MAX_HINTS = 8
_MAX_SEARCH_DIAGNOSTIC_COUNT = 10_000
_MAX_SEARCH_DIAGNOSTIC_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_MEMBER_PATH_BYTES = 1024
_MAX_SOURCE_MEMBER_DEPTH = 8
_MAX_TARGET_STEM_BYTES = 160
_MAX_TARGET_NAME_BYTES = 255
_MAX_ORDINAL = (1 << 63) - 1
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SNAPSHOT_ID = re.compile(r"^candidate-snapshot-v1:[0-9a-f]{64}$")
_SEMANTIC_SNAPSHOT_ID = re.compile(
    r"^candidate-snapshot-v2:[0-9a-f]{64}$"
)
_SEMANTIC_INVENTORY_ID = re.compile(
    r"^folder-inventory-v2:[0-9a-f]{64}$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PLAN_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_URL_TOKEN = re.compile(r"(?i)(?:https?://|www\.|magnet:\?)")
_FORBIDDEN_COMPONENT_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)
_PLAN_FIELDS = frozenset(
    {
        "archives",
        "candidate_snapshot_id",
        "config_revision_id",
        "created_at",
        "limits",
        "members",
        "parser_version",
        "inspector_version",
        "policy_version",
        "provider_version",
        "rejected_entries",
        "run_id",
        "schema_version",
        "source_folder",
        "source_root",
        "tmdb_id",
        "work_type",
    }
)
_ROOT_FIELDS = frozenset({"device", "inode", "path"})
_FOLDER_FIELDS = frozenset(
    {"device", "folder_generation_id", "inode", "name"}
)
_PLAN_V2_FIELDS = frozenset(
    {
        "archives",
        "candidate_snapshot",
        "config_revision",
        "config_revision_id",
        "created_at",
        "limits",
        "members",
        "parser_version",
        "inspector_version",
        "policy_version",
        "provider_version",
        "rejected_entries",
        "run_id",
        "schema_version",
        "source_folder",
        "source_root",
        "tmdb_id",
        "watch_id",
        "work_type",
    }
)
_ROOT_V2_FIELDS = frozenset({"path"})
_FOLDER_V2_FIELDS = frozenset(
    {"folder_generation_id", "inventory_id", "name"}
)
_CANDIDATE_SNAPSHOT_V2_FIELDS = frozenset(
    {"schema_version", "snapshot_id", "sources"}
)
_ARCHIVE_FIELDS = frozenset(
    {
        "archive_set_id",
        "format",
        "manifest_digest",
        "post_id",
        "release_id",
        "season_numbers",
        "thread_id",
        "volumes",
    }
)
_VOLUME_FIELDS = frozenset(
    {"attachment_id", "index", "sha256", "size_bytes"}
)
_MEMBER_FIELDS = frozenset(
    {
        "archive_set_id",
        "destination_name",
        "sha256",
        "size_bytes",
        "source_path",
    }
)
_REJECTED_FIELDS = frozenset(
    {"archive_set_id", "member_name_digest", "reason"}
)
_LIMIT_FIELDS = frozenset(
    {
        "max_archive_entries",
        "max_archive_sets",
        "max_archive_volume_bytes",
        "max_archive_volumes",
        "max_compression_ratio",
        "max_subtitle_member_bytes",
        "max_total_archive_bytes",
        "max_total_subtitle_bytes",
    }
)


def _invalid(
    code: ErrorCode = ErrorCode.INVALID_SUBTITLE_ACQUISITION_PLAN,
) -> DomainError:
    return DomainError(code)


def _require_int(
    value: object,
    *,
    minimum: int,
    maximum: int = _MAX_ORDINAL,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _require_string(value: object, *, max_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > max_bytes
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise _invalid()
    return value


def _opaque(value: object) -> str:
    if not isinstance(value, str) or _OPAQUE.fullmatch(value) is None:
        raise _invalid()
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise _invalid()
    return value


def _bounded_untrusted_text(value: object, *, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)
    normalized = unicodedata.normalize("NFKC", value)
    visible = "".join(
        char
        if not unicodedata.category(char).startswith("C")
        else "\N{REPLACEMENT CHARACTER}"
        for char in normalized
    ).strip()
    if _URL_TOKEN.search(visible) is not None:
        raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)
    encoded = visible.encode("utf-8")
    if len(encoded) > max_bytes:
        visible = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return visible


def _bounded_hints(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > _MAX_HINTS:
        raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)
    hints = tuple(
        _bounded_untrusted_text(item, max_bytes=_MAX_HINT_BYTES)
        for item in value
    )
    if len(set(hints)) != len(hints):
        raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)
    return hints


def _parse_prefixed_ordinal(value: object, *, prefix: str) -> int:
    if not isinstance(value, str):
        raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)
    match = re.fullmatch(rf"{re.escape(prefix)}:([1-9][0-9]*)", value)
    if match is None:
        raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)
    return _require_int(
        int(match.group(1)),
        minimum=1,
    )


@dataclass(frozen=True, slots=True, order=True)
class EmbeddedSubtitleTrackId:
    ordinal: int

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= _MAX_ORDINAL:
            raise _invalid(ErrorCode.INVALID_EMBEDDED_SUBTITLE_DATA)

    @classmethod
    def parse(cls, value: object) -> EmbeddedSubtitleTrackId:
        return cls(_parse_prefixed_ordinal(value, prefix="embedded-sub"))

    def __str__(self) -> str:
        return f"embedded-sub:{self.ordinal}"


@dataclass(frozen=True, slots=True, order=True)
class SubtitleReleaseId:
    ordinal: int

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= _MAX_ORDINAL:
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)

    @classmethod
    def parse(cls, value: object) -> SubtitleReleaseId:
        return cls(_parse_prefixed_ordinal(value, prefix="subrelease"))

    def __str__(self) -> str:
        return f"subrelease:{self.ordinal}"


@dataclass(frozen=True, slots=True, order=True)
class SubtitleArchiveSetId:
    ordinal: int

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= _MAX_ORDINAL:
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)

    @classmethod
    def parse(cls, value: object) -> SubtitleArchiveSetId:
        return cls(_parse_prefixed_ordinal(value, prefix="subarchive"))

    def __str__(self) -> str:
        return f"subarchive:{self.ordinal}"


@dataclass(frozen=True, slots=True, order=True)
class SubtitleSearchCursorId:
    ordinal: int

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= _MAX_ORDINAL:
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)

    @classmethod
    def parse(cls, value: object) -> SubtitleSearchCursorId:
        return cls(_parse_prefixed_ordinal(value, prefix="subcursor"))

    def __str__(self) -> str:
        return f"subcursor:{self.ordinal}"


class EmbeddedSubtitleProbeStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    INDETERMINATE = "indeterminate"


class EmbeddedChineseStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class EmbeddedSubtitleCodec(StrEnum):
    ASS = "ass"
    SUBRIP = "subrip"
    PGS = "pgs"
    WEBVTT = "webvtt"
    DVB = "dvb"
    MOV_TEXT = "mov_text"
    OTHER = "other"
    UNKNOWN = "unknown"


class EmbeddedSubtitleLanguage(StrEnum):
    ZH_HANS = "zh-hans"
    ZH_HANT = "zh-hant"
    ZH = "zh"
    JA = "ja"
    EN = "en"
    OTHER = "other"
    UNKNOWN = "unknown"

    @property
    def is_chinese(self) -> bool:
        return self in {
            EmbeddedSubtitleLanguage.ZH_HANS,
            EmbeddedSubtitleLanguage.ZH_HANT,
            EmbeddedSubtitleLanguage.ZH,
        }


@dataclass(frozen=True, slots=True)
class EmbeddedSubtitleTrack:
    track_id: EmbeddedSubtitleTrackId
    codec: EmbeddedSubtitleCodec
    language: EmbeddedSubtitleLanguage
    default: bool
    forced: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.track_id, EmbeddedSubtitleTrackId)
            or not isinstance(self.codec, EmbeddedSubtitleCodec)
            or not isinstance(self.language, EmbeddedSubtitleLanguage)
            or type(self.default) is not bool
            or type(self.forced) is not bool
        ):
            raise _invalid(ErrorCode.INVALID_EMBEDDED_SUBTITLE_DATA)


@dataclass(frozen=True, slots=True)
class EmbeddedSubtitleInspection:
    video_id: CandidateId
    season_number: int
    probe_status: EmbeddedSubtitleProbeStatus
    chinese_status: EmbeddedChineseStatus
    tracks: tuple[EmbeddedSubtitleTrack, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.video_id, CandidateId)
            or self.video_id.kind is not CandidateKind.VIDEO
            or type(self.season_number) is not int
            or not 0 <= self.season_number <= 999
            or not isinstance(self.probe_status, EmbeddedSubtitleProbeStatus)
            or not isinstance(self.chinese_status, EmbeddedChineseStatus)
            or not isinstance(self.tracks, tuple)
            or len(self.tracks) > MAX_EMBEDDED_SUBTITLE_TRACKS
            or any(not isinstance(item, EmbeddedSubtitleTrack) for item in self.tracks)
            or tuple(sorted(self.tracks, key=lambda item: item.track_id))
            != self.tracks
            or len({item.track_id for item in self.tracks}) != len(self.tracks)
        ):
            raise _invalid(ErrorCode.INVALID_EMBEDDED_SUBTITLE_DATA)
        has_chinese = any(item.language.is_chinese for item in self.tracks)
        has_unknown = any(
            item.language is EmbeddedSubtitleLanguage.UNKNOWN
            for item in self.tracks
        )
        expected_chinese = (
            EmbeddedChineseStatus.PRESENT
            if has_chinese
            else (
                EmbeddedChineseStatus.ABSENT
                if self.probe_status
                in {
                    EmbeddedSubtitleProbeStatus.PRESENT,
                    EmbeddedSubtitleProbeStatus.ABSENT,
                }
                and not has_unknown
                else EmbeddedChineseStatus.UNKNOWN
            )
        )
        if (
            self.probe_status is EmbeddedSubtitleProbeStatus.PRESENT
            and not self.tracks
        ) or (
            self.probe_status is EmbeddedSubtitleProbeStatus.ABSENT
            and self.tracks
        ) or self.chinese_status is not expected_chinese:
            raise _invalid(ErrorCode.INVALID_EMBEDDED_SUBTITLE_DATA)


class SubtitleArchiveFormat(StrEnum):
    ZIP = "zip"
    SEVEN_Z = "7z"
    RAR = "rar"


@dataclass(frozen=True, slots=True)
class SubtitleArchiveSetSummary:
    archive_set_id: SubtitleArchiveSetId
    format: SubtitleArchiveFormat
    volume_count: int
    declared_size: int
    label_hint: str = ""
    coverage_hint: str = ""
    language_hints: tuple[str, ...] = ()
    release_group_hints: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.archive_set_id, SubtitleArchiveSetId)
            or not isinstance(self.format, SubtitleArchiveFormat)
            or type(self.volume_count) is not int
            or not 1 <= self.volume_count <= MAX_ARCHIVE_VOLUMES
            or type(self.declared_size) is not int
            or not 0 <= self.declared_size <= MAX_TOTAL_ARCHIVE_BYTES
            or (
                self.format is not SubtitleArchiveFormat.RAR
                and self.volume_count != 1
            )
        ):
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)
        object.__setattr__(
            self,
            "label_hint",
            _bounded_untrusted_text(self.label_hint, max_bytes=_MAX_TEXT_BYTES),
        )
        object.__setattr__(
            self,
            "coverage_hint",
            _bounded_untrusted_text(
                self.coverage_hint,
                max_bytes=_MAX_HINT_BYTES,
            ),
        )
        for field in ("language_hints", "release_group_hints", "warnings"):
            object.__setattr__(self, field, _bounded_hints(getattr(self, field)))


@dataclass(frozen=True, slots=True)
class SubtitleReleaseSummary:
    release_id: SubtitleReleaseId
    archive_sets: tuple[SubtitleArchiveSetSummary, ...]
    title: str
    post_excerpt: str
    coverage_hint: str
    language_hints: tuple[str, ...]
    release_group_hints: tuple[str, ...]
    match_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    evidence_complete: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.release_id, SubtitleReleaseId)
            or not isinstance(self.archive_sets, tuple)
            or not 1 <= len(self.archive_sets) <= MAX_ARCHIVE_SETS
            or any(
                not isinstance(item, SubtitleArchiveSetSummary)
                for item in self.archive_sets
            )
            or len({item.archive_set_id for item in self.archive_sets})
            != len(self.archive_sets)
            or type(self.evidence_complete) is not bool
        ):
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)
        object.__setattr__(
            self,
            "title",
            _bounded_untrusted_text(self.title, max_bytes=_MAX_TEXT_BYTES),
        )
        object.__setattr__(
            self,
            "post_excerpt",
            _bounded_untrusted_text(
                self.post_excerpt,
                max_bytes=_MAX_EXCERPT_BYTES,
            ),
        )
        object.__setattr__(
            self,
            "coverage_hint",
            _bounded_untrusted_text(
                self.coverage_hint,
                max_bytes=_MAX_HINT_BYTES,
            ),
        )
        for field in (
            "language_hints",
            "release_group_hints",
            "match_reasons",
            "warnings",
        ):
            object.__setattr__(self, field, _bounded_hints(getattr(self, field)))


@dataclass(frozen=True, slots=True)
class SubtitleSearchPage:
    items: tuple[SubtitleReleaseSummary, ...]
    next_cursor: SubtitleSearchCursorId | None
    complete: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.items, tuple)
            or len(self.items) > MAX_SEARCH_RESULTS_PER_PAGE
            or any(not isinstance(item, SubtitleReleaseSummary) for item in self.items)
            or len({item.release_id for item in self.items}) != len(self.items)
            or (
                self.next_cursor is not None
                and not isinstance(self.next_cursor, SubtitleSearchCursorId)
            )
            or type(self.complete) is not bool
            or (self.complete and self.next_cursor is not None)
        ):
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)


@dataclass(frozen=True, slots=True, order=True)
class SubtitleArchiveSetCapability:
    """Stable forum identity; signed attachment URLs are deliberately absent."""

    archive_set_id: SubtitleArchiveSetId
    release_id: SubtitleReleaseId
    format: SubtitleArchiveFormat
    thread_id: int
    post_id: int
    attachment_ids: tuple[int, ...]
    declared_size: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.archive_set_id, SubtitleArchiveSetId)
            or not isinstance(self.release_id, SubtitleReleaseId)
            or not isinstance(self.format, SubtitleArchiveFormat)
            or type(self.thread_id) is not int
            or self.thread_id < 1
            or type(self.post_id) is not int
            or self.post_id < 1
            or not isinstance(self.attachment_ids, tuple)
            or not 1 <= len(self.attachment_ids) <= MAX_ARCHIVE_VOLUMES
            or any(
                type(item) is not int or item < 1
                for item in self.attachment_ids
            )
            or len(set(self.attachment_ids)) != len(self.attachment_ids)
            or type(self.declared_size) is not int
            or not 0 <= self.declared_size <= MAX_TOTAL_ARCHIVE_BYTES
            or (
                self.format is not SubtitleArchiveFormat.RAR
                and len(self.attachment_ids) != 1
            )
        ):
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)


@dataclass(frozen=True, slots=True)
class SubtitleSearchRecord:
    season_number: int
    cursor: SubtitleSearchCursorId | None
    page: SubtitleSearchPage

    def __post_init__(self) -> None:
        if (
            type(self.season_number) is not int
            or not 0 <= self.season_number <= 999
            or (
                self.cursor is not None
                and not isinstance(self.cursor, SubtitleSearchCursorId)
            )
            or not isinstance(self.page, SubtitleSearchPage)
        ):
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)


class SubtitleSearchEmptyStage(StrEnum):
    NOT_EMPTY = "not_empty"
    FORUM_SEARCH = "forum_search"
    NATIVE_ATTACHMENT = "native_attachment"
    ARCHIVE_FILTER = "archive_filter"
    RELEASE_FILTER = "release_filter"


@dataclass(frozen=True, slots=True)
class SubtitleSearchDiagnostics:
    """Bounded, URL-free counters for diagnosing successful searches."""

    query_aliases: tuple[str, ...]
    alias_thread_counts: tuple[int, ...]
    discovered_thread_count: int
    fetched_thread_count: int
    fetched_thread_page_count: int
    parsed_post_count: int
    native_attachment_count: int
    selectable_archive_set_count: int
    release_count: int

    def __post_init__(self) -> None:
        aliases = self.query_aliases
        counts = self.alias_thread_counts
        counters = (
            self.discovered_thread_count,
            self.fetched_thread_count,
            self.fetched_thread_page_count,
            self.parsed_post_count,
            self.native_attachment_count,
            self.selectable_archive_set_count,
            self.release_count,
        )
        if (
            not isinstance(aliases, tuple)
            or not 1 <= len(aliases) <= 3
            or any(
                not isinstance(alias, str)
                or not alias.strip()
                or len(alias.encode("utf-8")) > _MAX_TEXT_BYTES
                or _URL_TOKEN.search(alias) is not None
                or any(
                    unicodedata.category(char).startswith("C")
                    for char in alias
                )
                for alias in aliases
            )
            or len(
                {
                    unicodedata.normalize("NFKC", alias).casefold()
                    for alias in aliases
                }
            )
            != len(aliases)
            or not isinstance(counts, tuple)
            or len(counts) != len(aliases)
            or any(
                type(count) is not int
                or not 0 <= count <= _MAX_SEARCH_DIAGNOSTIC_COUNT
                for count in counts
            )
            or any(
                type(count) is not int
                or not 0 <= count <= _MAX_SEARCH_DIAGNOSTIC_COUNT
                for count in counters
            )
            or self.discovered_thread_count > sum(counts)
            or self.fetched_thread_count > self.discovered_thread_count
            or self.release_count > self.selectable_archive_set_count
        ):
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)

    @property
    def empty_stage(self) -> SubtitleSearchEmptyStage:
        if self.release_count:
            return SubtitleSearchEmptyStage.NOT_EMPTY
        if not self.discovered_thread_count:
            return SubtitleSearchEmptyStage.FORUM_SEARCH
        if not self.native_attachment_count:
            return SubtitleSearchEmptyStage.NATIVE_ATTACHMENT
        if not self.selectable_archive_set_count:
            return SubtitleSearchEmptyStage.ARCHIVE_FILTER
        return SubtitleSearchEmptyStage.RELEASE_FILTER


class SubtitleSearchFailureCode(StrEnum):
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    RESPONSE_TOO_LARGE = "response_too_large"
    BUDGET_EXCEEDED = "budget_exceeded"
    CHALLENGE_OR_LOGIN = "challenge_or_login"
    PARSER_DRIFT = "parser_drift"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    PROVIDER_MISSING = "provider_missing"
    PROVIDER_VERSION_MISMATCH = "provider_version_mismatch"
    QUERY_COMPILATION_FAILED = "query_compilation_failed"
    INVALID_PROVIDER_RESULT = "invalid_provider_result"


class SubtitleSearchFailureStage(StrEnum):
    PROVIDER_SETUP = "provider_setup"
    QUERY_COMPILATION = "query_compilation"
    SEARCH_LANDING = "search_landing"
    FORUM_SEARCH = "forum_search"
    THREAD_FETCH = "thread_fetch"
    RESULT_VALIDATION = "result_validation"


@dataclass(frozen=True, slots=True)
class SubtitleSearchFailureDiagnostics:
    """Bounded, URL-free evidence for one failed provider search."""

    error_code: SubtitleSearchFailureCode
    stage: SubtitleSearchFailureStage
    retryable: bool
    query_aliases: tuple[str, ...] = ()
    query_alias_index: int | None = None
    http_response_count: int = 0
    received_html_bytes: int = 0
    http_status: int | None = None

    def __post_init__(self) -> None:
        aliases = self.query_aliases
        if (
            not isinstance(self.error_code, SubtitleSearchFailureCode)
            or not isinstance(self.stage, SubtitleSearchFailureStage)
            or type(self.retryable) is not bool
            or not isinstance(aliases, tuple)
            or len(aliases) > MAX_SUBTITLE_SEARCH_ALIASES
            or any(
                not isinstance(alias, str)
                or not alias.strip()
                or len(alias.encode("utf-8")) > _MAX_TEXT_BYTES
                or _URL_TOKEN.search(alias) is not None
                or any(
                    unicodedata.category(char).startswith("C")
                    for char in alias
                )
                for alias in aliases
            )
            or len(
                {
                    unicodedata.normalize("NFKC", alias).casefold()
                    for alias in aliases
                }
            )
            != len(aliases)
            or (
                self.query_alias_index is not None
                and (
                    type(self.query_alias_index) is not int
                    or not 0 <= self.query_alias_index < len(aliases)
                )
            )
            or type(self.http_response_count) is not int
            or not 0
            <= self.http_response_count
            <= _MAX_SEARCH_DIAGNOSTIC_COUNT
            or type(self.received_html_bytes) is not int
            or not 0
            <= self.received_html_bytes
            <= _MAX_SEARCH_DIAGNOSTIC_BYTES
            or (
                self.http_status is not None
                and (
                    type(self.http_status) is not int
                    or not 100 <= self.http_status <= 599
                )
            )
        ):
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)


@dataclass(frozen=True, slots=True, order=True)
class SubtitleSelection:
    season_number: int
    archive_set_id: SubtitleArchiveSetId

    def __post_init__(self) -> None:
        if (
            type(self.season_number) is not int
            or not 0 <= self.season_number <= 999
            or not isinstance(self.archive_set_id, SubtitleArchiveSetId)
        ):
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SELECTION)


class SubtitleSelectionStatus(StrEnum):
    SELECTED = "selected"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True, slots=True)
class SubtitleSelectionDecision:
    status: SubtitleSelectionStatus
    selections: tuple[SubtitleSelection, ...]
    reason_code: str | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status, SubtitleSelectionStatus)
            or not isinstance(self.selections, tuple)
            or len(self.selections) > MAX_ARCHIVE_SETS
            or any(not isinstance(item, SubtitleSelection) for item in self.selections)
            or tuple(sorted(self.selections)) != self.selections
            or len({item.season_number for item in self.selections})
            != len(self.selections)
        ):
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SELECTION)
        if self.status is SubtitleSelectionStatus.SELECTED:
            if not self.selections or self.reason_code is not None:
                raise _invalid(ErrorCode.INVALID_SUBTITLE_SELECTION)
        elif self.selections or self.reason_code is None:
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SELECTION)
        if self.reason_code is not None and _OPAQUE.fullmatch(self.reason_code) is None:
            raise _invalid(ErrorCode.INVALID_SUBTITLE_SELECTION)

    @classmethod
    def selected(
        cls,
        selections: tuple[SubtitleSelection, ...],
    ) -> SubtitleSelectionDecision:
        return cls(
            status=SubtitleSelectionStatus.SELECTED,
            selections=tuple(sorted(selections)),
            reason_code=None,
        )

    @classmethod
    def needs_attention(cls, reason_code: str) -> SubtitleSelectionDecision:
        return cls(
            status=SubtitleSelectionStatus.NEEDS_ATTENTION,
            selections=(),
            reason_code=reason_code,
        )


@dataclass(frozen=True, slots=True, order=True)
class SubtitleArchiveVolume:
    index: int
    attachment_id: int
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.index) is not int
            or not 1 <= self.index <= MAX_ARCHIVE_VOLUMES
            or type(self.attachment_id) is not int
            or self.attachment_id < 1
            or type(self.size_bytes) is not int
            or not 1 <= self.size_bytes <= MAX_ARCHIVE_VOLUME_BYTES
        ):
            raise _invalid()
        object.__setattr__(self, "sha256", _digest(self.sha256))


@dataclass(frozen=True, slots=True)
class SubtitleArchiveSource:
    release_id: SubtitleReleaseId
    archive_set_id: SubtitleArchiveSetId
    format: SubtitleArchiveFormat
    season_numbers: tuple[int, ...]
    thread_id: int
    post_id: int
    manifest_digest: str
    volumes: tuple[SubtitleArchiveVolume, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.release_id, SubtitleReleaseId)
            or not isinstance(self.archive_set_id, SubtitleArchiveSetId)
            or not isinstance(self.format, SubtitleArchiveFormat)
            or not isinstance(self.season_numbers, tuple)
            or not self.season_numbers
            or tuple(sorted(set(self.season_numbers))) != self.season_numbers
            or any(
                type(item) is not int or not 0 <= item <= 999
                for item in self.season_numbers
            )
            or type(self.thread_id) is not int
            or self.thread_id < 1
            or type(self.post_id) is not int
            or self.post_id < 1
            or not isinstance(self.volumes, tuple)
            or not 1 <= len(self.volumes) <= MAX_ARCHIVE_VOLUMES
            or any(not isinstance(item, SubtitleArchiveVolume) for item in self.volumes)
            or tuple(item.index for item in self.volumes)
            != tuple(range(1, len(self.volumes) + 1))
            or len({item.attachment_id for item in self.volumes})
            != len(self.volumes)
            or (
                self.format is not SubtitleArchiveFormat.RAR
                and len(self.volumes) != 1
            )
        ):
            raise _invalid()
        object.__setattr__(self, "manifest_digest", _digest(self.manifest_digest))


def _member_path(value: object) -> PurePosixPath:
    path = (
        value
        if isinstance(value, PurePosixPath)
        else PurePosixPath(value)
        if isinstance(value, str)
        else None
    )
    if (
        path is None
        or path.is_absolute()
        or not path.parts
        or len(path.parts) > _MAX_SOURCE_MEMBER_DEPTH
        or ".." in path.parts
        or any(
            not part
            or part in {".", ".."}
            or "\\" in part
            or any(
                char in _FORBIDDEN_COMPONENT_CHARACTERS
                for char in part
            )
            or unicodedata.normalize("NFKC", part).casefold().startswith(".env")
            or any(unicodedata.category(char).startswith("C") for char in part)
            for part in path.parts
        )
        or len(path.as_posix().encode("utf-8")) > _MAX_SOURCE_MEMBER_PATH_BYTES
    ):
        raise _invalid()
    return path


def _safe_member_stem(path: PurePosixPath) -> str:
    stem = unicodedata.normalize("NFKC", path.stem).strip(" .")
    if (
        not stem
        or any(
            char in _FORBIDDEN_COMPONENT_CHARACTERS
            or unicodedata.category(char).startswith("C")
            for char in stem
        )
        or stem.upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise _invalid()
    encoded = stem.encode("utf-8")
    if len(encoded) > _MAX_TARGET_STEM_BYTES:
        stem = encoded[:_MAX_TARGET_STEM_BYTES].decode(
            "utf-8", errors="ignore"
        ).rstrip(" .")
    if not stem:
        raise _invalid()
    return stem


def _destination_name(
    *,
    archive_set_id: SubtitleArchiveSetId,
    source_path: PurePosixPath,
    sha256: str,
) -> str:
    extension = source_path.suffix.lower()
    if extension not in SUBTITLE_EXTENSIONS:
        raise _invalid()
    name = (
        f"{_safe_member_stem(source_path)}--a{archive_set_id.ordinal}-"
        f"{sha256[:12]}{extension}"
    )
    if len(name.encode("utf-8")) > _MAX_TARGET_NAME_BYTES:
        raise _invalid()
    return name


@dataclass(frozen=True, slots=True)
class InspectedSubtitleMember:
    archive_set_id: SubtitleArchiveSetId
    source_path: PurePosixPath
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.archive_set_id, SubtitleArchiveSetId)
            or type(self.size_bytes) is not int
            or not 1 <= self.size_bytes <= MAX_SUBTITLE_MEMBER_BYTES
        ):
            raise _invalid()
        object.__setattr__(self, "source_path", _member_path(self.source_path))
        object.__setattr__(self, "sha256", _digest(self.sha256))
        _destination_name(
            archive_set_id=self.archive_set_id,
            source_path=self.source_path,
            sha256=self.sha256,
        )


@dataclass(frozen=True, slots=True)
class PlannedSubtitleMember:
    archive_set_id: SubtitleArchiveSetId
    source_path: PurePosixPath
    destination_name: str
    size_bytes: int
    sha256: str

    @classmethod
    def from_inspected(
        cls,
        member: InspectedSubtitleMember,
    ) -> PlannedSubtitleMember:
        return cls(
            archive_set_id=member.archive_set_id,
            source_path=member.source_path,
            destination_name=_destination_name(
                archive_set_id=member.archive_set_id,
                source_path=member.source_path,
                sha256=member.sha256,
            ),
            size_bytes=member.size_bytes,
            sha256=member.sha256,
        )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.archive_set_id, SubtitleArchiveSetId)
            or type(self.size_bytes) is not int
            or not 1 <= self.size_bytes <= MAX_SUBTITLE_MEMBER_BYTES
        ):
            raise _invalid()
        object.__setattr__(self, "source_path", _member_path(self.source_path))
        object.__setattr__(self, "sha256", _digest(self.sha256))
        expected = _destination_name(
            archive_set_id=self.archive_set_id,
            source_path=self.source_path,
            sha256=self.sha256,
        )
        if self.destination_name != expected:
            raise _invalid()


class RejectedArchiveEntryReason(StrEnum):
    UNSUPPORTED_TYPE = "unsupported_type"
    NESTED_ARCHIVE = "nested_archive"
    UNSAFE_PATH = "unsafe_path"
    ENCRYPTED = "encrypted"
    SPECIAL_FILE = "special_file"
    ENV_PATH = "env_path"
    DUPLICATE_NAME = "duplicate_name"
    SIZE_LIMIT = "size_limit"


@dataclass(frozen=True, slots=True, order=True)
class RejectedArchiveEntry:
    archive_set_id: SubtitleArchiveSetId
    member_name_digest: str
    reason: RejectedArchiveEntryReason

    def __post_init__(self) -> None:
        if (
            not isinstance(self.archive_set_id, SubtitleArchiveSetId)
            or not isinstance(self.reason, RejectedArchiveEntryReason)
        ):
            raise _invalid()
        object.__setattr__(
            self,
            "member_name_digest",
            _digest(self.member_name_digest),
        )


def _canonical_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _invalid()
    try:
        if value.utcoffset() is None:
            raise _invalid()
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except DomainError:
        raise
    except Exception:
        raise _invalid() from None


def _component(value: object) -> str:
    value = _require_string(value, max_bytes=255)
    if (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
        or value.casefold().startswith(".env")
    ):
        raise _invalid()
    return value


def _root_payload(root: RootBinding) -> dict[str, object]:
    return {
        "device": root.device,
        "inode": root.inode,
        "path": root.path.as_posix(),
    }


def _parse_root(value: object) -> RootBinding:
    payload = check_fields(value, _ROOT_FIELDS, field="source_root")
    path = payload["path"]
    if not isinstance(path, str):
        raise _invalid()
    return RootBinding(
        path=PurePosixPath(path),
        device=_require_int(payload["device"], minimum=0),
        inode=_require_int(payload["inode"], minimum=0),
    )


def _duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise _invalid()
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise _invalid() from None
    if _canonical_timestamp(parsed) != value:
        raise _invalid()
    return parsed


@dataclass(frozen=True, slots=True, init=False)
class SubtitleAcquisitionPlan:
    schema_version: str
    policy_version: str
    provider_version: str
    parser_version: str
    inspector_version: str
    run_id: str
    config_revision_id: str
    created_at: str
    source_root: RootBinding
    source_folder: str
    source_folder_device: int
    source_folder_inode: int
    folder_generation_id: str
    candidate_snapshot_id: str
    work_type: TmdbWorkType
    tmdb_id: int
    archives: tuple[SubtitleArchiveSource, ...]
    members: tuple[PlannedSubtitleMember, ...]
    rejected_entries: tuple[RejectedArchiveEntry, ...]
    plan_hash: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        config_revision_id: str,
        created_at: datetime,
        source_root: RootBinding,
        source_folder: str,
        source_folder_device: int,
        source_folder_inode: int,
        folder_generation_id: str,
        candidate_snapshot_id: str,
        tmdb_id: int,
        archives: tuple[SubtitleArchiveSource, ...],
        inspected_members: tuple[InspectedSubtitleMember, ...],
        rejected_entries: tuple[RejectedArchiveEntry, ...] = (),
    ) -> SubtitleAcquisitionPlan:
        if (
            not isinstance(run_id, str)
            or not run_id
            or len(run_id.encode("utf-8")) > _MAX_RUN_ID_BYTES
            or any(unicodedata.category(char).startswith("C") for char in run_id)
            or not isinstance(source_root, RootBinding)
            or type(source_folder_device) is not int
            or source_folder_device < 0
            or type(source_folder_inode) is not int
            or source_folder_inode < 0
            or not isinstance(candidate_snapshot_id, str)
            or _SNAPSHOT_ID.fullmatch(candidate_snapshot_id) is None
            or type(tmdb_id) is not int
            or tmdb_id < 1
            or not isinstance(archives, tuple)
            or not 1 <= len(archives) <= MAX_ARCHIVE_SETS
            or any(not isinstance(item, SubtitleArchiveSource) for item in archives)
            or not isinstance(inspected_members, tuple)
            or not 1 <= len(inspected_members) <= MAX_ARCHIVE_ENTRIES
            or any(
                not isinstance(item, InspectedSubtitleMember)
                for item in inspected_members
            )
            or not isinstance(rejected_entries, tuple)
            or any(
                not isinstance(item, RejectedArchiveEntry)
                for item in rejected_entries
            )
            or len(inspected_members) + len(rejected_entries) > MAX_ARCHIVE_ENTRIES
        ):
            raise _invalid()
        config_revision_id = _opaque(config_revision_id)
        folder_generation_id = _opaque(folder_generation_id)
        source_folder = _component(source_folder)
        canonical_archives = tuple(
            sorted(archives, key=lambda item: item.archive_set_id)
        )
        archive_ids = tuple(item.archive_set_id for item in canonical_archives)
        seasons = tuple(
            season
            for archive in canonical_archives
            for season in archive.season_numbers
        )
        if (
            len(set(archive_ids)) != len(archive_ids)
            or len(set(seasons)) != len(seasons)
            or sum(
                volume.size_bytes
                for archive in canonical_archives
                for volume in archive.volumes
            )
            > MAX_TOTAL_ARCHIVE_BYTES
        ):
            raise _invalid()
        archive_id_set = set(archive_ids)
        canonical_inspected = tuple(
            sorted(
                inspected_members,
                key=lambda item: (
                    item.archive_set_id,
                    filesystem_name_key(item.source_path.as_posix()),
                    item.source_path.as_posix(),
                ),
            )
        )
        members = tuple(
            PlannedSubtitleMember.from_inspected(item)
            for item in canonical_inspected
        )
        if (
            any(item.archive_set_id not in archive_id_set for item in members)
            or set(item.archive_set_id for item in members) != archive_id_set
            or len(
                {(item.archive_set_id, item.source_path) for item in members}
            )
            != len(members)
            or len({filesystem_name_key(item.destination_name) for item in members})
            != len(members)
        ):
            raise _invalid(ErrorCode.SUBTITLE_ACQUISITION_COLLISION)
        total_member_bytes = sum(item.size_bytes for item in members)
        total_archive_bytes = sum(
            volume.size_bytes
            for archive in canonical_archives
            for volume in archive.volumes
        )
        if (
            total_member_bytes > MAX_TOTAL_SUBTITLE_BYTES
            or total_member_bytes > total_archive_bytes * MAX_COMPRESSION_RATIO
        ):
            raise _invalid()
        canonical_rejected = tuple(sorted(rejected_entries))
        if (
            any(
                item.archive_set_id not in archive_id_set
                for item in canonical_rejected
            )
            or len(set(canonical_rejected)) != len(canonical_rejected)
        ):
            raise _invalid()

        plan = object.__new__(cls)
        object.__setattr__(
            plan,
            "schema_version",
            CURRENT_SUBTITLE_ACQUISITION_SCHEMA_VERSION,
        )
        object.__setattr__(
            plan,
            "policy_version",
            CURRENT_SUBTITLE_ACQUISITION_POLICY_VERSION,
        )
        object.__setattr__(
            plan,
            "provider_version",
            CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION,
        )
        object.__setattr__(
            plan,
            "parser_version",
            CURRENT_SUBTITLE_SEARCH_PARSER_VERSION,
        )
        object.__setattr__(
            plan,
            "inspector_version",
            CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION,
        )
        object.__setattr__(plan, "run_id", run_id)
        object.__setattr__(plan, "config_revision_id", config_revision_id)
        object.__setattr__(plan, "created_at", _canonical_timestamp(created_at))
        object.__setattr__(plan, "source_root", source_root)
        object.__setattr__(plan, "source_folder", source_folder)
        object.__setattr__(plan, "source_folder_device", source_folder_device)
        object.__setattr__(plan, "source_folder_inode", source_folder_inode)
        object.__setattr__(plan, "folder_generation_id", folder_generation_id)
        object.__setattr__(plan, "candidate_snapshot_id", candidate_snapshot_id)
        object.__setattr__(plan, "work_type", TmdbWorkType.ANIME)
        object.__setattr__(plan, "tmdb_id", tmdb_id)
        object.__setattr__(plan, "archives", canonical_archives)
        object.__setattr__(plan, "members", members)
        object.__setattr__(plan, "rejected_entries", canonical_rejected)
        object.__setattr__(plan, "plan_hash", "")
        object.__setattr__(
            plan,
            "plan_hash",
            "sha256:" + hashlib.sha256(plan.canonical_bytes()).hexdigest(),
        )
        return plan

    @property
    def destination_directory(self) -> PurePosixPath:
        return PurePosixPath(
            f"reeloom-acquired-{self.plan_hash.removeprefix('sha256:')}"
        )

    def canonical_bytes(self) -> bytes:
        payload = {
            "archives": [
                {
                    "archive_set_id": str(archive.archive_set_id),
                    "format": archive.format.value,
                    "manifest_digest": archive.manifest_digest,
                    "post_id": archive.post_id,
                    "release_id": str(archive.release_id),
                    "season_numbers": list(archive.season_numbers),
                    "thread_id": archive.thread_id,
                    "volumes": [
                        {
                            "attachment_id": volume.attachment_id,
                            "index": volume.index,
                            "sha256": volume.sha256,
                            "size_bytes": volume.size_bytes,
                        }
                        for volume in archive.volumes
                    ],
                }
                for archive in self.archives
            ],
            "candidate_snapshot_id": self.candidate_snapshot_id,
            "config_revision_id": self.config_revision_id,
            "created_at": self.created_at,
            "limits": {
                "max_archive_entries": MAX_ARCHIVE_ENTRIES,
                "max_archive_sets": MAX_ARCHIVE_SETS,
                "max_archive_volume_bytes": MAX_ARCHIVE_VOLUME_BYTES,
                "max_archive_volumes": MAX_ARCHIVE_VOLUMES,
                "max_compression_ratio": MAX_COMPRESSION_RATIO,
                "max_subtitle_member_bytes": MAX_SUBTITLE_MEMBER_BYTES,
                "max_total_archive_bytes": MAX_TOTAL_ARCHIVE_BYTES,
                "max_total_subtitle_bytes": MAX_TOTAL_SUBTITLE_BYTES,
            },
            "members": [
                {
                    "archive_set_id": str(member.archive_set_id),
                    "destination_name": member.destination_name,
                    "sha256": member.sha256,
                    "size_bytes": member.size_bytes,
                    "source_path": member.source_path.as_posix(),
                }
                for member in self.members
            ],
            "parser_version": self.parser_version,
            "inspector_version": self.inspector_version,
            "policy_version": self.policy_version,
            "provider_version": self.provider_version,
            "rejected_entries": [
                {
                    "archive_set_id": str(item.archive_set_id),
                    "member_name_digest": item.member_name_digest,
                    "reason": item.reason.value,
                }
                for item in self.rejected_entries
            ],
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "source_folder": {
                "device": self.source_folder_device,
                "folder_generation_id": self.folder_generation_id,
                "inode": self.source_folder_inode,
                "name": self.source_folder,
            },
            "source_root": _root_payload(self.source_root),
            "tmdb_id": self.tmdb_id,
            "work_type": self.work_type.value,
        }
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    def verify_hash(self) -> bool:
        expected = "sha256:" + hashlib.sha256(
            self.canonical_bytes()
        ).hexdigest()
        return (
            _PLAN_HASH.fullmatch(self.plan_hash) is not None
            and hmac.compare_digest(self.plan_hash, expected)
        )

    @classmethod
    def from_canonical_bytes(
        cls,
        content: bytes,
        *,
        plan_hash: str,
    ) -> SubtitleAcquisitionPlan:
        if (
            not isinstance(content, bytes)
            or not 0 < len(content) <= _MAX_CANONICAL_BYTES
            or not isinstance(plan_hash, str)
            or _PLAN_HASH.fullmatch(plan_hash) is None
            or not hmac.compare_digest(
                plan_hash,
                "sha256:" + hashlib.sha256(content).hexdigest(),
            )
        ):
            raise _invalid()
        try:
            raw = check_fields(
                json.loads(content, object_pairs_hook=_duplicate_pairs),
                _PLAN_FIELDS,
                field="subtitle_acquisition_plan",
            )
            limits = check_fields(raw["limits"], _LIMIT_FIELDS, field="limits")
            if limits != {
                "max_archive_entries": MAX_ARCHIVE_ENTRIES,
                "max_archive_sets": MAX_ARCHIVE_SETS,
                "max_archive_volume_bytes": MAX_ARCHIVE_VOLUME_BYTES,
                "max_archive_volumes": MAX_ARCHIVE_VOLUMES,
                "max_compression_ratio": MAX_COMPRESSION_RATIO,
                "max_subtitle_member_bytes": MAX_SUBTITLE_MEMBER_BYTES,
                "max_total_archive_bytes": MAX_TOTAL_ARCHIVE_BYTES,
                "max_total_subtitle_bytes": MAX_TOTAL_SUBTITLE_BYTES,
            }:
                raise _invalid()
            folder = check_fields(
                raw["source_folder"],
                _FOLDER_FIELDS,
                field="source_folder",
            )
            archives = tuple(
                _parse_archive(item, index=index)
                for index, item in enumerate(_require_list(raw["archives"]))
            )
            parsed_members = tuple(
                _parse_member(item, index=index)
                for index, item in enumerate(_require_list(raw["members"]))
            )
            rejected = tuple(
                _parse_rejected(item, index=index)
                for index, item in enumerate(
                    _require_list(raw["rejected_entries"])
                )
            )
            if (
                raw["schema_version"]
                != CURRENT_SUBTITLE_ACQUISITION_SCHEMA_VERSION
                or raw["policy_version"]
                != CURRENT_SUBTITLE_ACQUISITION_POLICY_VERSION
                or raw["provider_version"]
                != CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION
                or raw["parser_version"]
                != CURRENT_SUBTITLE_SEARCH_PARSER_VERSION
                or raw["inspector_version"]
                != CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION
                or raw["work_type"] != TmdbWorkType.ANIME.value
            ):
                raise _invalid()
            plan = cls.create(
                run_id=raw["run_id"],  # type: ignore[arg-type]
                config_revision_id=raw[
                    "config_revision_id"
                ],  # type: ignore[arg-type]
                created_at=_parse_timestamp(raw["created_at"]),
                source_root=_parse_root(raw["source_root"]),
                source_folder=folder["name"],  # type: ignore[arg-type]
                source_folder_device=folder["device"],  # type: ignore[arg-type]
                source_folder_inode=folder["inode"],  # type: ignore[arg-type]
                folder_generation_id=folder[
                    "folder_generation_id"
                ],  # type: ignore[arg-type]
                candidate_snapshot_id=raw[
                    "candidate_snapshot_id"
                ],  # type: ignore[arg-type]
                tmdb_id=raw["tmdb_id"],  # type: ignore[arg-type]
                archives=archives,
                inspected_members=tuple(
                    InspectedSubtitleMember(
                        archive_set_id=item.archive_set_id,
                        source_path=item.source_path,
                        size_bytes=item.size_bytes,
                        sha256=item.sha256,
                    )
                    for item in parsed_members
                ),
                rejected_entries=rejected,
            )
        except (
            DomainError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            raise _invalid() from None
        if (
            plan.plan_hash != plan_hash
            or plan.canonical_bytes() != content
            or tuple(item.destination_name for item in parsed_members)
            != tuple(item.destination_name for item in plan.members)
        ):
            raise _invalid()
        return plan


@dataclass(frozen=True, slots=True, init=False)
class SubtitleAcquisitionPlanV2:
    """Canonical M14.5 subtitle plan bound only to semantic identity."""

    schema_version: str
    policy_version: str
    provider_version: str
    parser_version: str
    inspector_version: str
    run_id: str
    config_revision: int
    config_revision_id: str
    watch_id: str
    created_at: str
    source_root: SemanticRootBinding
    source_folder: str
    folder_generation_id: str
    inventory_id: str
    candidate_snapshot: SemanticCandidateSnapshot
    work_type: TmdbWorkType
    tmdb_id: int
    archives: tuple[SubtitleArchiveSource, ...]
    members: tuple[PlannedSubtitleMember, ...]
    rejected_entries: tuple[RejectedArchiveEntry, ...]
    plan_hash: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        config_revision: int,
        config_revision_id: str,
        watch_id: str,
        created_at: datetime,
        source_root: SemanticRootBinding,
        source_folder: str,
        folder_generation_id: str,
        inventory_id: str,
        candidate_snapshot: SemanticCandidateSnapshot,
        tmdb_id: int,
        archives: tuple[SubtitleArchiveSource, ...],
        inspected_members: tuple[InspectedSubtitleMember, ...],
        rejected_entries: tuple[RejectedArchiveEntry, ...] = (),
    ) -> SubtitleAcquisitionPlanV2:
        if (
            not isinstance(run_id, str)
            or not run_id
            or len(run_id.encode("utf-8")) > _MAX_RUN_ID_BYTES
            or any(unicodedata.category(char).startswith("C") for char in run_id)
            or type(config_revision) is not int
            or config_revision < 1
            or not isinstance(source_root, SemanticRootBinding)
            or not isinstance(inventory_id, str)
            or _SEMANTIC_INVENTORY_ID.fullmatch(inventory_id) is None
            or not isinstance(candidate_snapshot, SemanticCandidateSnapshot)
            or _SEMANTIC_SNAPSHOT_ID.fullmatch(candidate_snapshot.snapshot_id)
            is None
            or not candidate_snapshot.sources
            or not any(
                item.kind is CandidateKind.VIDEO
                for item in candidate_snapshot.sources
            )
            or type(tmdb_id) is not int
            or tmdb_id < 1
            or not isinstance(archives, tuple)
            or not 1 <= len(archives) <= MAX_ARCHIVE_SETS
            or any(not isinstance(item, SubtitleArchiveSource) for item in archives)
            or not isinstance(inspected_members, tuple)
            or not 1 <= len(inspected_members) <= MAX_ARCHIVE_ENTRIES
            or any(
                not isinstance(item, InspectedSubtitleMember)
                for item in inspected_members
            )
            or not isinstance(rejected_entries, tuple)
            or any(
                not isinstance(item, RejectedArchiveEntry)
                for item in rejected_entries
            )
            or len(inspected_members) + len(rejected_entries)
            > MAX_ARCHIVE_ENTRIES
        ):
            raise _invalid()
        config_revision_id = _opaque(config_revision_id)
        watch_id = _opaque(watch_id)
        folder_generation_id = _opaque(folder_generation_id)
        source_folder = _component(source_folder)
        canonical_archives = tuple(
            sorted(archives, key=lambda item: item.archive_set_id)
        )
        archive_ids = tuple(item.archive_set_id for item in canonical_archives)
        seasons = tuple(
            season
            for archive in canonical_archives
            for season in archive.season_numbers
        )
        if (
            len(set(archive_ids)) != len(archive_ids)
            or len(set(seasons)) != len(seasons)
            or sum(
                volume.size_bytes
                for archive in canonical_archives
                for volume in archive.volumes
            )
            > MAX_TOTAL_ARCHIVE_BYTES
        ):
            raise _invalid()
        archive_id_set = set(archive_ids)
        canonical_inspected = tuple(
            sorted(
                inspected_members,
                key=lambda item: (
                    item.archive_set_id,
                    filesystem_name_key(item.source_path.as_posix()),
                    item.source_path.as_posix(),
                ),
            )
        )
        members = tuple(
            PlannedSubtitleMember.from_inspected(item)
            for item in canonical_inspected
        )
        if (
            any(item.archive_set_id not in archive_id_set for item in members)
            or set(item.archive_set_id for item in members) != archive_id_set
            or len(
                {(item.archive_set_id, item.source_path) for item in members}
            )
            != len(members)
            or len(
                {filesystem_name_key(item.destination_name) for item in members}
            )
            != len(members)
        ):
            raise _invalid(ErrorCode.SUBTITLE_ACQUISITION_COLLISION)
        total_member_bytes = sum(item.size_bytes for item in members)
        total_archive_bytes = sum(
            volume.size_bytes
            for archive in canonical_archives
            for volume in archive.volumes
        )
        if (
            total_member_bytes > MAX_TOTAL_SUBTITLE_BYTES
            or total_member_bytes
            > total_archive_bytes * MAX_COMPRESSION_RATIO
        ):
            raise _invalid()
        canonical_rejected = tuple(sorted(rejected_entries))
        if (
            any(
                item.archive_set_id not in archive_id_set
                for item in canonical_rejected
            )
            or len(set(canonical_rejected)) != len(canonical_rejected)
        ):
            raise _invalid()

        plan = object.__new__(cls)
        object.__setattr__(
            plan,
            "schema_version",
            CURRENT_SUBTITLE_ACQUISITION_SCHEMA_VERSION_V2,
        )
        object.__setattr__(
            plan,
            "policy_version",
            CURRENT_SUBTITLE_ACQUISITION_POLICY_VERSION,
        )
        object.__setattr__(
            plan,
            "provider_version",
            CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION,
        )
        object.__setattr__(
            plan,
            "parser_version",
            CURRENT_SUBTITLE_SEARCH_PARSER_VERSION,
        )
        object.__setattr__(
            plan,
            "inspector_version",
            CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION,
        )
        object.__setattr__(plan, "run_id", run_id)
        object.__setattr__(plan, "config_revision", config_revision)
        object.__setattr__(plan, "config_revision_id", config_revision_id)
        object.__setattr__(plan, "watch_id", watch_id)
        object.__setattr__(plan, "created_at", _canonical_timestamp(created_at))
        object.__setattr__(plan, "source_root", source_root)
        object.__setattr__(plan, "source_folder", source_folder)
        object.__setattr__(plan, "folder_generation_id", folder_generation_id)
        object.__setattr__(plan, "inventory_id", inventory_id)
        object.__setattr__(plan, "candidate_snapshot", candidate_snapshot)
        object.__setattr__(plan, "work_type", TmdbWorkType.ANIME)
        object.__setattr__(plan, "tmdb_id", tmdb_id)
        object.__setattr__(plan, "archives", canonical_archives)
        object.__setattr__(plan, "members", members)
        object.__setattr__(plan, "rejected_entries", canonical_rejected)
        object.__setattr__(plan, "plan_hash", "")
        object.__setattr__(
            plan,
            "plan_hash",
            "sha256:" + hashlib.sha256(plan.canonical_bytes()).hexdigest(),
        )
        return plan

    @property
    def candidate_snapshot_id(self) -> str:
        return self.candidate_snapshot.snapshot_id

    @property
    def destination_directory(self) -> PurePosixPath:
        return PurePosixPath(
            f"reeloom-acquired-{self.plan_hash.removeprefix('sha256:')}"
        )

    def canonical_bytes(self) -> bytes:
        payload = {
            "archives": [
                {
                    "archive_set_id": str(archive.archive_set_id),
                    "format": archive.format.value,
                    "manifest_digest": archive.manifest_digest,
                    "post_id": archive.post_id,
                    "release_id": str(archive.release_id),
                    "season_numbers": list(archive.season_numbers),
                    "thread_id": archive.thread_id,
                    "volumes": [
                        {
                            "attachment_id": volume.attachment_id,
                            "index": volume.index,
                            "sha256": volume.sha256,
                            "size_bytes": volume.size_bytes,
                        }
                        for volume in archive.volumes
                    ],
                }
                for archive in self.archives
            ],
            "candidate_snapshot": {
                "schema_version": self.candidate_snapshot.schema_version,
                "snapshot_id": self.candidate_snapshot.snapshot_id,
                "sources": self.candidate_snapshot.payload(),
            },
            "config_revision": self.config_revision,
            "config_revision_id": self.config_revision_id,
            "created_at": self.created_at,
            "limits": {
                "max_archive_entries": MAX_ARCHIVE_ENTRIES,
                "max_archive_sets": MAX_ARCHIVE_SETS,
                "max_archive_volume_bytes": MAX_ARCHIVE_VOLUME_BYTES,
                "max_archive_volumes": MAX_ARCHIVE_VOLUMES,
                "max_compression_ratio": MAX_COMPRESSION_RATIO,
                "max_subtitle_member_bytes": MAX_SUBTITLE_MEMBER_BYTES,
                "max_total_archive_bytes": MAX_TOTAL_ARCHIVE_BYTES,
                "max_total_subtitle_bytes": MAX_TOTAL_SUBTITLE_BYTES,
            },
            "members": [
                {
                    "archive_set_id": str(member.archive_set_id),
                    "destination_name": member.destination_name,
                    "sha256": member.sha256,
                    "size_bytes": member.size_bytes,
                    "source_path": member.source_path.as_posix(),
                }
                for member in self.members
            ],
            "parser_version": self.parser_version,
            "inspector_version": self.inspector_version,
            "policy_version": self.policy_version,
            "provider_version": self.provider_version,
            "rejected_entries": [
                {
                    "archive_set_id": str(item.archive_set_id),
                    "member_name_digest": item.member_name_digest,
                    "reason": item.reason.value,
                }
                for item in self.rejected_entries
            ],
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "source_folder": {
                "folder_generation_id": self.folder_generation_id,
                "inventory_id": self.inventory_id,
                "name": self.source_folder,
            },
            "source_root": self.source_root.payload(),
            "tmdb_id": self.tmdb_id,
            "watch_id": self.watch_id,
            "work_type": self.work_type.value,
        }
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    def verify_hash(self) -> bool:
        expected = "sha256:" + hashlib.sha256(
            self.canonical_bytes()
        ).hexdigest()
        return (
            _PLAN_HASH.fullmatch(self.plan_hash) is not None
            and hmac.compare_digest(self.plan_hash, expected)
        )

    @classmethod
    def from_canonical_bytes(
        cls,
        content: bytes,
        *,
        plan_hash: str,
    ) -> SubtitleAcquisitionPlanV2:
        if (
            not isinstance(content, bytes)
            or not 0 < len(content) <= _MAX_CANONICAL_BYTES
            or not isinstance(plan_hash, str)
            or _PLAN_HASH.fullmatch(plan_hash) is None
            or not hmac.compare_digest(
                plan_hash,
                "sha256:" + hashlib.sha256(content).hexdigest(),
            )
        ):
            raise _invalid()
        try:
            raw = check_fields(
                json.loads(content, object_pairs_hook=_duplicate_pairs),
                _PLAN_V2_FIELDS,
                field="subtitle_acquisition_plan_v2",
            )
            if (
                raw["schema_version"]
                != CURRENT_SUBTITLE_ACQUISITION_SCHEMA_VERSION_V2
                or raw["policy_version"]
                != CURRENT_SUBTITLE_ACQUISITION_POLICY_VERSION
                or raw["provider_version"]
                != CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION
                or raw["parser_version"]
                != CURRENT_SUBTITLE_SEARCH_PARSER_VERSION
                or raw["inspector_version"]
                != CURRENT_SUBTITLE_ARCHIVE_INSPECTOR_VERSION
                or raw["work_type"] != TmdbWorkType.ANIME.value
            ):
                raise _invalid()
            limits = check_fields(raw["limits"], _LIMIT_FIELDS, field="limits")
            if limits != {
                "max_archive_entries": MAX_ARCHIVE_ENTRIES,
                "max_archive_sets": MAX_ARCHIVE_SETS,
                "max_archive_volume_bytes": MAX_ARCHIVE_VOLUME_BYTES,
                "max_archive_volumes": MAX_ARCHIVE_VOLUMES,
                "max_compression_ratio": MAX_COMPRESSION_RATIO,
                "max_subtitle_member_bytes": MAX_SUBTITLE_MEMBER_BYTES,
                "max_total_archive_bytes": MAX_TOTAL_ARCHIVE_BYTES,
                "max_total_subtitle_bytes": MAX_TOTAL_SUBTITLE_BYTES,
            }:
                raise _invalid()
            root = check_fields(
                raw["source_root"], _ROOT_V2_FIELDS, field="source_root"
            )
            folder = check_fields(
                raw["source_folder"],
                _FOLDER_V2_FIELDS,
                field="source_folder",
            )
            snapshot = check_fields(
                raw["candidate_snapshot"],
                _CANDIDATE_SNAPSHOT_V2_FIELDS,
                field="candidate_snapshot",
            )
            if snapshot["schema_version"] != "2":
                raise _invalid()
            candidate_snapshot = SemanticCandidateSnapshot.from_payload(
                snapshot["sources"],
                snapshot_id=snapshot["snapshot_id"],
            )
            archives = tuple(
                _parse_archive(item, index=index)
                for index, item in enumerate(_require_list(raw["archives"]))
            )
            parsed_members = tuple(
                _parse_member(item, index=index)
                for index, item in enumerate(_require_list(raw["members"]))
            )
            rejected = tuple(
                _parse_rejected(item, index=index)
                for index, item in enumerate(
                    _require_list(raw["rejected_entries"])
                )
            )
            path = root["path"]
            if not isinstance(path, str):
                raise _invalid()
            plan = cls.create(
                run_id=raw["run_id"],  # type: ignore[arg-type]
                config_revision=_require_int(
                    raw["config_revision"], minimum=1
                ),
                config_revision_id=raw[
                    "config_revision_id"
                ],  # type: ignore[arg-type]
                watch_id=raw["watch_id"],  # type: ignore[arg-type]
                created_at=_parse_timestamp(raw["created_at"]),
                source_root=SemanticRootBinding(PurePosixPath(path)),
                source_folder=folder["name"],  # type: ignore[arg-type]
                folder_generation_id=folder[
                    "folder_generation_id"
                ],  # type: ignore[arg-type]
                inventory_id=folder["inventory_id"],  # type: ignore[arg-type]
                candidate_snapshot=candidate_snapshot,
                tmdb_id=raw["tmdb_id"],  # type: ignore[arg-type]
                archives=archives,
                inspected_members=tuple(
                    InspectedSubtitleMember(
                        archive_set_id=item.archive_set_id,
                        source_path=item.source_path,
                        size_bytes=item.size_bytes,
                        sha256=item.sha256,
                    )
                    for item in parsed_members
                ),
                rejected_entries=rejected,
            )
        except (
            DomainError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            raise _invalid() from None
        if (
            plan.plan_hash != plan_hash
            or plan.canonical_bytes() != content
            or tuple(item.destination_name for item in parsed_members)
            != tuple(item.destination_name for item in plan.members)
        ):
            raise _invalid()
        return plan

def _require_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise _invalid()
    return value


def _parse_archive(value: object, *, index: int) -> SubtitleArchiveSource:
    payload = check_fields(value, _ARCHIVE_FIELDS, field=f"archives[{index}]")
    try:
        return SubtitleArchiveSource(
            release_id=SubtitleReleaseId.parse(payload["release_id"]),
            archive_set_id=SubtitleArchiveSetId.parse(payload["archive_set_id"]),
            format=SubtitleArchiveFormat(payload["format"]),
            season_numbers=tuple(
                _require_int(item, minimum=0, maximum=999)
                for item in _require_list(payload["season_numbers"])
            ),
            thread_id=_require_int(payload["thread_id"], minimum=1),
            post_id=_require_int(payload["post_id"], minimum=1),
            manifest_digest=_digest(payload["manifest_digest"]),
            volumes=tuple(
                _parse_volume(item, index=volume_index)
                for volume_index, item in enumerate(_require_list(payload["volumes"]))
            ),
        )
    except (TypeError, ValueError):
        raise _invalid() from None


def _parse_volume(value: object, *, index: int) -> SubtitleArchiveVolume:
    payload = check_fields(value, _VOLUME_FIELDS, field=f"volumes[{index}]")
    return SubtitleArchiveVolume(
        index=_require_int(payload["index"], minimum=1, maximum=MAX_ARCHIVE_VOLUMES),
        attachment_id=_require_int(payload["attachment_id"], minimum=1),
        size_bytes=_require_int(
            payload["size_bytes"],
            minimum=1,
            maximum=MAX_ARCHIVE_VOLUME_BYTES,
        ),
        sha256=_digest(payload["sha256"]),
    )


def _parse_member(value: object, *, index: int) -> PlannedSubtitleMember:
    payload = check_fields(value, _MEMBER_FIELDS, field=f"members[{index}]")
    return PlannedSubtitleMember(
        archive_set_id=SubtitleArchiveSetId.parse(payload["archive_set_id"]),
        source_path=_member_path(payload["source_path"]),
        destination_name=_require_string(
            payload["destination_name"],
            max_bytes=_MAX_TARGET_NAME_BYTES,
        ),
        size_bytes=_require_int(
            payload["size_bytes"],
            minimum=1,
            maximum=MAX_SUBTITLE_MEMBER_BYTES,
        ),
        sha256=_digest(payload["sha256"]),
    )


def _parse_rejected(value: object, *, index: int) -> RejectedArchiveEntry:
    payload = check_fields(
        value,
        _REJECTED_FIELDS,
        field=f"rejected_entries[{index}]",
    )
    try:
        reason = RejectedArchiveEntryReason(payload["reason"])
    except ValueError:
        raise _invalid() from None
    return RejectedArchiveEntry(
        archive_set_id=SubtitleArchiveSetId.parse(payload["archive_set_id"]),
        member_name_digest=_digest(payload["member_name_digest"]),
        reason=reason,
    )


def verify_subtitle_acquisition_plan_bytes(
    content: bytes,
    plan_hash: str,
) -> bool:
    for plan_type in (SubtitleAcquisitionPlanV2, SubtitleAcquisitionPlan):
        try:
            plan_type.from_canonical_bytes(content, plan_hash=plan_hash)
        except (DomainError, ValueError):
            continue
        return True
    return False
