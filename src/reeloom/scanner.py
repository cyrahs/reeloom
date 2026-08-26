"""Intake discovery.

Each stable direct child folder of a watch root is one run. Stability matters
because CloudDrive-style offline downloads materialize a folder's files in
batches: a folder is only handed to a run once its shape has stopped changing
for the configured window.
"""

from __future__ import annotations

import logging
import os
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from reeloom.models import FileKind, ReeloomError, SnapshotFile
from reeloom.subtitles import detect_variant_for_file

_LOGGER = logging.getLogger(__name__)

ARCHIVE_BUCKET = "archive"
FAIL_BUCKET = "fail"
IN_PROGRESS_BUCKET = "in_progress"
"""CloudDrive2 offline downloads land here until reeloom moves the finished
item out; an incomplete download must never become a run."""
RESERVED_NAMES = frozenset({ARCHIVE_BUCKET, FAIL_BUCKET, IN_PROGRESS_BUCKET})

ACQUIRED_DIR = ".acquired"
"""Staging folder under ``archive/<folder>/`` for subtitles reeloom downloads."""

VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mp4", ".ts", ".webm"})
SUBTITLE_EXTENSIONS = frozenset({".ass", ".srt", ".ssa", ".sup", ".vtt"})

MAX_FILES_PER_FOLDER = 5_000
MAX_DEPTH = 8


class ScanError(ReeloomError):
    pass


def classify(filename: str) -> FileKind:
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return FileKind.VIDEO
    if suffix in SUBTITLE_EXTENSIONS:
        return FileKind.SUBTITLE
    return FileKind.OTHER


def name_key(value: str) -> str:
    """Case- and width-insensitive key for comparing filesystem names."""

    return unicodedata.normalize("NFKC", value).casefold()


def safe_relative(value: str) -> PurePosixPath:
    """Validate a root-relative path.

    Rejects absolute paths, parent traversal, backslashes and ``.env*`` so a
    stored path can never address anything outside its root.
    """

    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or any("\\" in part for part in path.parts)
        or any(part.casefold().startswith(".env") for part in path.parts)
    ):
        raise ScanError("unsafe_path", path=value)
    return path


def discover_folders(root: Path) -> list[str]:
    """Direct child folders eligible for intake.

    Buckets, hidden entries, loose files and symlinks are excluded; a folder
    containing a ``.env*`` entry at any depth is skipped entirely.
    """

    if not root.is_dir():
        raise ScanError("watch_root_missing", root=str(root))

    names: list[str] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.name.casefold() in RESERVED_NAMES:
                continue
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            names.append(entry.name)
    return sorted(names)


@dataclass(frozen=True, slots=True)
class FolderShape:
    """Cheap fingerprint used to decide whether a folder has settled."""

    file_count: int
    total_bytes: int
    newest_mtime_ns: int


def folder_shape(path: Path) -> FolderShape:
    count = 0
    total = 0
    newest = 0
    for _, stat in _walk(path):
        count += 1
        total += stat.st_size
        newest = max(newest, stat.st_mtime_ns)
    return FolderShape(file_count=count, total_bytes=total, newest_mtime_ns=newest)


def snapshot_folder(path: Path) -> list[SnapshotFile]:
    """Build the immutable candidate list the Agent will work from.

    Ordinals are assigned per kind after a stable sort, so the same folder
    always produces the same candidate IDs.
    """

    entries = sorted(_walk(path), key=lambda item: (item[0].casefold(), item[0]))
    if len(entries) > MAX_FILES_PER_FOLDER:
        raise ScanError("too_many_files", count=len(entries))

    ordinals = {FileKind.VIDEO: 0, FileKind.SUBTITLE: 0, FileKind.OTHER: 0}
    prefix = {FileKind.VIDEO: "V", FileKind.SUBTITLE: "S", FileKind.OTHER: "O"}
    files: list[SnapshotFile] = []
    for relative, stat in entries:
        kind = classify(relative)
        ordinals[kind] += 1
        files.append(
            SnapshotFile(
                candidate_id=f"{prefix[kind]}{ordinals[kind]}",
                relative_path=relative,
                kind=kind,
                size_bytes=stat.st_size,
                variant=(
                    detect_variant_for_file(path / relative)
                    if kind is FileKind.SUBTITLE
                    else None
                ),
            )
        )
    return files


def _walk(path: Path) -> list[tuple[str, os.stat_result]]:
    """Collect regular files, never following symlinks."""

    collected: list[tuple[str, os.stat_result]] = []
    stack: list[tuple[Path, str, int]] = [(path, "", 0)]
    while stack:
        current, prefix, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise ScanError("too_deep", path=str(current))
        try:
            entries = list(os.scandir(current))
        except FileNotFoundError:
            continue
        for entry in entries:
            if entry.name.casefold().startswith(".env"):
                raise ScanError("env_file_present", path=entry.path)
            if entry.is_symlink():
                _LOGGER.debug("skipping symlink %s", entry.path)
                continue
            relative = f"{prefix}{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), f"{relative}/", depth + 1))
            elif entry.is_file(follow_symlinks=False):
                collected.append((relative, entry.stat(follow_symlinks=False)))
    return collected


class StabilityTracker:
    """Remembers when each folder last changed shape.

    Deliberately in-memory: a restart just makes folders wait one more window,
    which is always safe, and it keeps a table out of the schema.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._seen: dict[tuple[str, str], tuple[FolderShape, float]] = {}

    def observe(self, key: tuple[str, str], shape: FolderShape) -> float:
        """Record the current shape and return how long it has held."""

        now = self._clock()
        previous = self._seen.get(key)
        if previous is None or previous[0] != shape:
            self._seen[key] = (shape, now)
            return 0.0
        return now - previous[1]

    def is_stable(
        self, key: tuple[str, str], shape: FolderShape, required_seconds: int
    ) -> bool:
        if shape.file_count == 0:
            return False
        return self.observe(key, shape) >= required_seconds

    def forget(self, key: tuple[str, str]) -> None:
        self._seen.pop(key, None)
