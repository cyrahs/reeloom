from __future__ import annotations

from pathlib import PurePosixPath

from reeloom.kernel.candidates import CandidateKind

VIDEO_EXTENSIONS = frozenset(
    {".avi", ".m4v", ".mkv", ".mp4", ".ts", ".webm"}
)
SUBTITLE_EXTENSIONS = frozenset(
    {".ass", ".srt", ".ssa", ".sup", ".vtt"}
)


def candidate_kind_for_filename(filename: str) -> CandidateKind | None:
    """Classify only the final suffix, case-insensitively."""

    suffix = PurePosixPath(filename).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return CandidateKind.VIDEO
    if suffix in SUBTITLE_EXTENSIONS:
        return CandidateKind.SUBTITLE
    return None
