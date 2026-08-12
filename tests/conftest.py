from __future__ import annotations

import os
from pathlib import Path

import pytest

from reeloom.models import MediaType, WatchConfig


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """An inbound watch root and a library root on the same filesystem."""

    inbound = tmp_path / "inbound"
    library = tmp_path / "library"
    inbound.mkdir()
    library.mkdir()
    return inbound, library


@pytest.fixture
def config(roots: tuple[Path, Path]) -> WatchConfig:
    inbound, library = roots
    return WatchConfig(
        id="00000000-0000-0000-0000-000000000001",
        name="anime",
        inbound_root=str(inbound),
        library_root=str(library),
        media_type=MediaType.ANIME,
        stability_seconds=0,
    )


def make_files(root: Path, *relative: str, size: int = 16) -> None:
    for item in relative:
        path = root / item
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)


@pytest.fixture
def postgres_dsn() -> str:
    dsn = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return dsn


def _has_sevenzip() -> bool:
    from reeloom.adapters.archive import find_sevenzip

    return find_sevenzip() is not None


def _has_ffmpeg() -> bool:
    import shutil

    from reeloom.adapters.ffprobe import find_ffprobe

    return shutil.which("ffmpeg") is not None and find_ffprobe() is not None


_BINARY_CHECKS = {"7z": _has_sevenzip, "ffmpeg": _has_ffmpeg}


def pytest_collection_modifyitems(config, items) -> None:
    """Real-binary tests skip only where skipping is allowed.

    Locally a missing tool skips its ``binaries``-marked tests; with
    ``REELOOM_TEST_REQUIRE_BINARIES=1`` (the CI binaries job) no skip is
    added, so a broken toolchain install fails loudly instead of silently
    shrinking the suite.
    """

    if os.environ.get("REELOOM_TEST_REQUIRE_BINARIES") == "1":
        return
    available = {tool: check() for tool, check in _BINARY_CHECKS.items()}
    for item in items:
        marker = item.get_closest_marker("binaries")
        if marker is None:
            continue
        tool = marker.args[0]
        if not available.get(tool, False):
            item.add_marker(pytest.mark.skip(reason=f"{tool} is not installed"))
