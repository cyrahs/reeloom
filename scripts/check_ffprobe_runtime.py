"""Container smoke check for the ffprobe adapter.

Run inside the built image by the Container workflow: encodes a real test
video with the image's own ffmpeg, probes it through the actual adapter
code path, and asserts every field the replacement decision engine relies
on. Catches an image whose ffmpeg build lacks lavfi/mpeg4, whose ffprobe
rejects our fixed argv, or whose JSON stopped carrying the fields we read.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

from reeloom.adapters.ffprobe import find_ffprobe, probe_video


def render(path: Path, height: int) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x{height}:rate=24:duration=1",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def main() -> int:
    if find_ffprobe() is None:
        print("FAIL: ffprobe is not installed in the image", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as scratch:
        low = Path(scratch) / "low.mkv"
        high = Path(scratch) / "high.mp4"
        render(low, 240)
        render(high, 480)
        low_probe = asyncio.run(probe_video(low))
        high_probe = asyncio.run(probe_video(high))

    for name, probe, height in (("mkv", low_probe, 240), ("mp4", high_probe, 480)):
        assert probe is not None, f"{name}: probe returned None"
        assert probe.height == height, f"{name}: height {probe.height}"
        assert probe.video_codec == "mpeg4", f"{name}: codec {probe.video_codec}"
        assert probe.duration_seconds and 0.5 < probe.duration_seconds < 2.0, (
            f"{name}: duration {probe.duration_seconds}"
        )
        assert probe.bit_rate and probe.bit_rate > 0, (
            f"{name}: bit_rate {probe.bit_rate}"
        )

    assert high_probe.height > low_probe.height, "resolution ranking broken"
    print(f"ffprobe contract ok: {low_probe} / {high_probe}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
