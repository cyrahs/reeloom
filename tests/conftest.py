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
