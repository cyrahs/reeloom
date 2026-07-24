from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class ErrorCategory(StrEnum):
    """Stable high-level classes suitable for policy and user-facing handling."""

    INVALID_INPUT = "invalid_input"
    CONFLICT = "conflict"


class ErrorCode(StrEnum):
    """Machine-readable domain failures; callers must not parse exception text."""

    EXTRA_KEYS = "extra_keys"
    MISSING_KEYS = "missing_keys"
    INVALID_FIELD_TYPE = "invalid_field_type"
    INVALID_CANDIDATE_KIND = "invalid_candidate_kind"
    INVALID_CANDIDATE_ID = "invalid_candidate_id"
    CANDIDATE_KIND_MISMATCH = "candidate_kind_mismatch"
    DUPLICATE_CANDIDATE_ID = "duplicate_candidate_id"
    UNKNOWN_CANDIDATE_ID = "unknown_candidate_id"
    INVALID_EPISODE_CATALOG = "invalid_episode_catalog"
    INVALID_EPISODE_RANGE = "invalid_episode_range"
    SEASON_OUT_OF_BOUNDS = "season_out_of_bounds"
    EPISODE_OUT_OF_BOUNDS = "episode_out_of_bounds"
    DUPLICATE_VIDEO_MAPPING = "duplicate_video_mapping"
    EPISODE_RANGE_OVERLAP = "episode_range_overlap"
    DUPLICATE_SUBTITLE_MAPPING = "duplicate_subtitle_mapping"
    SUBTITLE_VIDEO_NOT_MAPPED = "subtitle_video_not_mapped"
    SUBTITLE_VARIANT_REQUIRED = "subtitle_variant_required"
    INVENTORY_CONFLICT = "inventory_conflict"
    INVALID_SERIES_TITLE = "invalid_series_title"
    INVALID_YEAR = "invalid_year"
    INVALID_TMDB_ID = "invalid_tmdb_id"
    INVALID_FILE_EXTENSION = "invalid_file_extension"
    INVALID_SUBTITLE_VARIANT = "invalid_subtitle_variant"
    INVALID_SPECIAL_KIND = "invalid_special_kind"
    INVALID_SPECIAL_EPISODE = "invalid_special_episode"
    DUPLICATE_SPECIAL_VIDEO = "duplicate_special_video"
    DUPLICATE_SPECIAL_EPISODE = "duplicate_special_episode"
    SPECIAL_EVIDENCE_CONFLICT = "special_evidence_conflict"
    INVALID_DESTINATION = "invalid_destination"
    DUPLICATE_PLAN_SOURCE = "duplicate_plan_source"
    DESTINATION_COLLISION = "destination_collision"
    PLAN_MAPPING_MISMATCH = "plan_mapping_mismatch"
    MISSING_PLAN_CANDIDATES = "missing_plan_candidates"
    INCOMPLETE_SOURCE_IDENTITY = "incomplete_source_identity"
    PLAN_PREFLIGHT_MISMATCH = "plan_preflight_mismatch"
    INVALID_APPROVAL = "invalid_approval"
    PATH_NOT_ABSOLUTE = "path_not_absolute"
    PATH_NOT_FOUND = "path_not_found"
    PATH_NOT_DIRECTORY = "path_not_directory"
    PATH_NOT_FILE = "path_not_file"
    PATH_ESCAPE = "path_escape"
    SYMLINK_NOT_ALLOWED = "symlink_not_allowed"
    ENV_PATH_FORBIDDEN = "env_path_forbidden"
    DUPLICATE_SCANNED_PATH = "duplicate_scanned_path"
    SCAN_FAILED = "scan_failed"
    SCAN_LIMIT_EXCEEDED = "scan_limit_exceeded"
    INVALID_TMDB_DATA = "invalid_tmdb_data"
    INVALID_TMDB_LANGUAGE = "invalid_tmdb_language"

    @property
    def category(self) -> ErrorCategory:
        if self in {
            ErrorCode.DUPLICATE_CANDIDATE_ID,
            ErrorCode.DUPLICATE_VIDEO_MAPPING,
            ErrorCode.EPISODE_RANGE_OVERLAP,
            ErrorCode.DUPLICATE_SUBTITLE_MAPPING,
            ErrorCode.INVENTORY_CONFLICT,
            ErrorCode.DUPLICATE_SPECIAL_VIDEO,
            ErrorCode.DUPLICATE_SPECIAL_EPISODE,
            ErrorCode.SPECIAL_EVIDENCE_CONFLICT,
            ErrorCode.DUPLICATE_PLAN_SOURCE,
            ErrorCode.DESTINATION_COLLISION,
            ErrorCode.PLAN_MAPPING_MISMATCH,
            ErrorCode.PLAN_PREFLIGHT_MISMATCH,
        }:
            return ErrorCategory.CONFLICT
        return ErrorCategory.INVALID_INPUT


class DomainError(ValueError):
    """A deterministic, structured failure emitted by the safety kernel."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        context: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.context = MappingProxyType(dict(context or {}))
        super().__init__(code.value)

    @property
    def category(self) -> ErrorCategory:
        return self.code.category
