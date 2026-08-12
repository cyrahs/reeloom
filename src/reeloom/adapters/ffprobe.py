"""Video probing.

Version replacement samples a few episodes from both sides and compares
resolution and bitrate. ffprobe runs as a subprocess with a fixed argv,
bounded output and a timeout, and its JSON is parsed defensively — a probe
that fails for any reason degrades to "no quality signal", never to a crash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from reeloom.models import ReeloomError

PROBE_TIMEOUT_SECONDS = 30
MAX_OUTPUT_BYTES = 512 * 1024

_LOGGER = logging.getLogger(__name__)


class ProbeError(ReeloomError):
    pass


@dataclass(frozen=True, slots=True)
class Probe:
    height: int | None
    duration_seconds: float | None
    bit_rate: int | None
    video_codec: str | None


def find_ffprobe() -> str | None:
    return shutil.which("ffprobe")


async def probe_video(path: Path) -> Probe | None:
    """Probe one video file. ``None`` means ffprobe is not installed."""

    executable = find_ffprobe()
    if executable is None:
        return None

    process = await asyncio.create_subprocess_exec(
        executable,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,height:format=duration,bit_rate",
        "-of",
        "json",
        str(path),
        stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), PROBE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ProbeError("probe_timeout", file=path.name) from None

    if process.returncode != 0:
        raise ProbeError(
            "probe_failed",
            file=path.name,
            detail=stderr.decode("utf-8", "replace")[:300],
        )
    if len(stdout) > MAX_OUTPUT_BYTES:
        raise ProbeError("probe_output_too_large", file=path.name)

    return _parse(stdout, path)


def _parse(stdout: bytes, path: Path) -> Probe:
    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as error:
        raise ProbeError("probe_bad_json", file=path.name) from error
    if not isinstance(payload, dict):
        raise ProbeError("probe_bad_json", file=path.name)

    height: int | None = None
    codec: str | None = None
    streams = payload.get("streams")
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if stream.get("codec_type") != "video":
                continue
            height = _as_int(stream.get("height"))
            raw_codec = stream.get("codec_name")
            codec = raw_codec if isinstance(raw_codec, str) else None
            break

    duration: float | None = None
    bit_rate: int | None = None
    container = payload.get("format")
    if isinstance(container, dict):
        duration = _as_float(container.get("duration"))
        bit_rate = _as_int(container.get("bit_rate"))

    return Probe(
        height=height,
        duration_seconds=duration,
        bit_rate=bit_rate,
        video_codec=codec,
    )


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
