from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.subtitle_acquisition import (
    MAX_SUBTITLE_SEARCH_ALIASES,
    MAX_SUBTITLE_SEARCH_ALIAS_BYTES,
    MAX_SEARCH_RESULTS_PER_PAGE,
    EmbeddedSubtitleInspection,
    InspectedSubtitleMember,
    PlannedSubtitleMember,
    RejectedArchiveEntry,
    SubtitleArchiveSource,
    SubtitleArchiveVolume,
    SubtitleArchiveSetCapability,
    SubtitleAcquisitionPlan,
    SubtitleSearchCursorId,
    SubtitleSearchDiagnostics,
    SubtitleSearchPage,
)

@dataclass(frozen=True, slots=True)
class SubtitleSearchRequest:
    """Provider-neutral input compiled from trusted selected TMDB data."""

    title_aliases: tuple[str, ...]
    season_number: int
    cursor: SubtitleSearchCursorId | None
    limit: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.title_aliases, tuple)
            or not 1 <= len(self.title_aliases) <= MAX_SUBTITLE_SEARCH_ALIASES
            or any(
                not isinstance(alias, str)
                or not alias.strip()
                or len(alias.encode("utf-8"))
                > MAX_SUBTITLE_SEARCH_ALIAS_BYTES
                or any(
                    unicodedata.category(char).startswith("C")
                    for char in alias
                )
                for alias in self.title_aliases
            )
            or len(
                {
                    unicodedata.normalize("NFKC", alias).casefold()
                    for alias in self.title_aliases
                }
            )
            != len(self.title_aliases)
            or type(self.season_number) is not int
            or not 0 <= self.season_number <= 999
            or (
                self.cursor is not None
                and not isinstance(self.cursor, SubtitleSearchCursorId)
            )
            or type(self.limit) is not int
            or not 1 <= self.limit <= MAX_SEARCH_RESULTS_PER_PAGE
        ):
            raise DomainError(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)


@dataclass(frozen=True, slots=True)
class SubtitleSearchResult:
    page: SubtitleSearchPage
    capabilities: tuple[SubtitleArchiveSetCapability, ...]
    diagnostics: SubtitleSearchDiagnostics

    def __post_init__(self) -> None:
        if (
            not isinstance(self.page, SubtitleSearchPage)
            or not isinstance(self.capabilities, tuple)
            or not isinstance(self.diagnostics, SubtitleSearchDiagnostics)
            or any(
                not isinstance(item, SubtitleArchiveSetCapability)
                for item in self.capabilities
            )
            or len(
                {item.archive_set_id for item in self.capabilities}
            )
            != len(self.capabilities)
            or self.diagnostics.release_count < len(self.page.items)
            or self.diagnostics.selectable_archive_set_count
            < len(self.capabilities)
        ):
            raise DomainError(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)
        summaries = {
            archive.archive_set_id: archive
            for release in self.page.items
            for archive in release.archive_sets
        }
        if set(summaries) != {
            item.archive_set_id for item in self.capabilities
        }:
            raise DomainError(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)
        for capability in self.capabilities:
            summary = summaries[capability.archive_set_id]
            if (
                capability.release_id
                not in {item.release_id for item in self.page.items}
                or capability.format is not summary.format
                or len(capability.attachment_ids) != summary.volume_count
                or capability.declared_size != summary.declared_size
            ):
                raise DomainError(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)


class SubtitleSearchErrorCode(StrEnum):
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    RESPONSE_TOO_LARGE = "response_too_large"
    BUDGET_EXCEEDED = "budget_exceeded"
    CHALLENGE_OR_LOGIN = "challenge_or_login"
    PARSER_DRIFT = "parser_drift"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"


class SubtitleSearchProviderError(RuntimeError):
    def __init__(
        self,
        code: SubtitleSearchErrorCode,
        *,
        retryable: bool,
    ) -> None:
        if not isinstance(code, SubtitleSearchErrorCode):
            raise TypeError("invalid subtitle search error code")
        self.code = code
        self.retryable = retryable
        super().__init__(code.value)


class SubtitleArchiveErrorCode(StrEnum):
    UNAVAILABLE = "unavailable"
    CAPABILITY_CHANGED = "capability_changed"
    DOWNLOAD_TOO_LARGE = "download_too_large"
    INVALID_FORMAT = "invalid_format"
    INVALID_MANIFEST = "invalid_manifest"
    ENCRYPTED = "encrypted"
    UNSAFE_ENTRY = "unsafe_entry"
    NESTED_ARCHIVE = "nested_archive"
    SPECIAL_FILE = "special_file"
    LIMIT_EXCEEDED = "limit_exceeded"
    CONTENT_DRIFT = "content_drift"


class SubtitleArchiveError(RuntimeError):
    def __init__(
        self,
        code: SubtitleArchiveErrorCode,
        *,
        retryable: bool,
    ) -> None:
        if not isinstance(code, SubtitleArchiveErrorCode):
            raise TypeError("invalid subtitle archive error code")
        self.code = code
        self.retryable = retryable
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class DownloadedArchiveVolume:
    volume: SubtitleArchiveVolume
    path: Path
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.volume, SubtitleArchiveVolume)
            or not isinstance(self.path, Path)
            or not self.path.is_absolute()
            or any(
                type(item) is not int or item < 0
                for item in (
                    self.device,
                    self.inode,
                    self.mtime_ns,
                    self.ctime_ns,
                )
            )
        ):
            raise DomainError(ErrorCode.INVALID_SUBTITLE_ACQUISITION_PLAN)


@dataclass(frozen=True, slots=True)
class DownloadedSubtitleArchiveSet:
    capability: SubtitleArchiveSetCapability
    volumes: tuple[DownloadedArchiveVolume, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.capability, SubtitleArchiveSetCapability)
            or not isinstance(self.volumes, tuple)
            or len(self.volumes) != len(self.capability.attachment_ids)
            or any(
                not isinstance(item, DownloadedArchiveVolume)
                for item in self.volumes
            )
            or tuple(item.volume.index for item in self.volumes)
            != tuple(range(1, len(self.volumes) + 1))
            or tuple(item.volume.attachment_id for item in self.volumes)
            != self.capability.attachment_ids
        ):
            raise DomainError(ErrorCode.INVALID_SUBTITLE_ACQUISITION_PLAN)


@dataclass(frozen=True, slots=True)
class InspectedSubtitleArchiveSet:
    source: SubtitleArchiveSource
    members: tuple[InspectedSubtitleMember, ...]
    rejected_entries: tuple[RejectedArchiveEntry, ...]

    def __post_init__(self) -> None:
        archive_id = getattr(self.source, "archive_set_id", None)
        if (
            not isinstance(self.source, SubtitleArchiveSource)
            or not isinstance(self.members, tuple)
            or not self.members
            or any(
                not isinstance(item, InspectedSubtitleMember)
                or item.archive_set_id != archive_id
                for item in self.members
            )
            or not isinstance(self.rejected_entries, tuple)
            or any(
                not isinstance(item, RejectedArchiveEntry)
                or item.archive_set_id != archive_id
                for item in self.rejected_entries
            )
        ):
            raise DomainError(ErrorCode.INVALID_SUBTITLE_ACQUISITION_PLAN)


class VideoSubtitleInspector(Protocol):
    """Read-only, snapshot-bound inspector for one opaque video candidate."""

    @property
    def snapshot_id(self) -> str: ...

    @property
    def candidate_count(self) -> int: ...

    async def inspect(
        self,
        video_id: CandidateId,
        *,
        season_number: int,
    ) -> EmbeddedSubtitleInspection: ...


class SubtitleSearchProvider(Protocol):
    """Fixed-purpose provider; implementations do not accept URLs or fids."""

    @property
    def provider_version(self) -> str: ...

    async def search(
        self,
        request: SubtitleSearchRequest,
    ) -> SubtitleSearchResult: ...


class SubtitleArchiveFetcher(Protocol):
    """Resolve native attachments into an isolated non-media workspace."""

    @property
    def provider_version(self) -> str: ...

    @property
    def parser_version(self) -> str: ...

    @property
    def workspace_root(self) -> Path: ...

    async def fetch(
        self,
        capability: SubtitleArchiveSetCapability,
    ) -> DownloadedSubtitleArchiveSet: ...


class SubtitleArchiveInspector(Protocol):
    """Inspect already-downloaded volumes without choosing output paths."""

    @property
    def inspector_version(self) -> str: ...

    async def inspect(
        self,
        downloaded: DownloadedSubtitleArchiveSet,
        *,
        season_numbers: tuple[int, ...],
    ) -> InspectedSubtitleArchiveSet: ...

    async def extract_member(
        self,
        downloaded: DownloadedSubtitleArchiveSet,
        member: PlannedSubtitleMember,
    ) -> bytes: ...


class SubtitleAcquisitionPlanStore(Protocol):
    """Content-addressed persistence isolated from media rename plans."""

    def save(self, plan: SubtitleAcquisitionPlan) -> None: ...

    def load(self, plan_hash: str) -> bytes: ...
