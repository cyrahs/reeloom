"""Archive extraction.

Subtitle releases arrive as .7z/.zip/.rar. Extraction runs 7-Zip as a
subprocess with a fixed argv, bounded output and a timeout, into a directory
Reeloom owns. Only subtitle files are kept, and member paths are checked so a
crafted archive cannot write outside the destination.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from reeloom.models import ReeloomError
from reeloom.scanner import SUBTITLE_EXTENSIONS

ARCHIVE_EXTENSIONS = frozenset({".7z", ".zip", ".rar"})
EXTRACT_TIMEOUT_SECONDS = 120
MAX_MEMBERS = 500
MAX_SUBTITLE_BYTES = 8 * 1024 * 1024

_LOGGER = logging.getLogger(__name__)


class ArchiveError(ReeloomError):
    pass


def find_sevenzip() -> str | None:
    for name in ("7zz", "7z", "7za"):
        found = shutil.which(name)
        if found:
            return found
    return None


def is_archive(filename: str) -> bool:
    return PurePosixPath(filename).suffix.lower() in ARCHIVE_EXTENSIONS


async def extract_subtitles(archive: Path, destination: Path) -> list[Path]:
    """Extract an archive and return the subtitle files it contained.

    Uses ``e`` (flatten) so nested folders in the archive cannot recreate a
    directory structure; every member lands directly in ``destination``.
    """

    executable = find_sevenzip()
    if executable is None:
        raise ArchiveError("sevenzip_not_installed")
    destination.mkdir(parents=True, exist_ok=True)

    process = await asyncio.create_subprocess_exec(
        executable,
        "e",
        "-y",
        "-bd",
        f"-o{destination}",
        str(archive),
        *[f"*{extension}" for extension in sorted(SUBTITLE_EXTENSIONS)],
        "-r",
        stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), EXTRACT_TIMEOUT_SECONDS
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ArchiveError("extract_timeout", archive=archive.name) from None

    if process.returncode not in (0, 1):
        raise ArchiveError(
            "extract_failed",
            archive=archive.name,
            detail=stderr.decode("utf-8", "replace")[:300],
        )

    found: list[Path] = []
    for path in sorted(destination.iterdir()):
        if len(found) >= MAX_MEMBERS:
            break
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue
        if path.stat().st_size > MAX_SUBTITLE_BYTES:
            _LOGGER.warning("skipping oversized subtitle %s", path.name)
            continue
        # Belt and braces: `e` flattens, so anything not directly under the
        # destination means the extractor did something unexpected.
        if path.parent != destination:
            raise ArchiveError("member_escaped_destination", member=path.name)
        found.append(path)
    return found
