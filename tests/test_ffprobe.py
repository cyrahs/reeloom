from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reeloom.adapters import ffprobe
from reeloom.adapters.ffprobe import Probe, ProbeError, _parse, probe_video

VIDEO = Path("Show S01E01.mkv")


def _payload(**overrides) -> bytes:
    payload = {
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": "hevc", "height": 1080},
        ],
        "format": {"duration": "1420.5", "bit_rate": "8000000"},
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def test_parse_reads_the_video_stream_and_format() -> None:
    assert _parse(_payload(), VIDEO) == Probe(
        height=1080,
        duration_seconds=1420.5,
        bit_rate=8_000_000,
        video_codec="hevc",
    )


def test_parse_tolerates_missing_fields() -> None:
    probe = _parse(b"{}", VIDEO)
    assert probe == Probe(
        height=None, duration_seconds=None, bit_rate=None, video_codec=None
    )


def test_parse_tolerates_garbage_values() -> None:
    probe = _parse(
        _payload(
            streams=[{"codec_type": "video", "height": "tall", "codec_name": 7}],
            format={"duration": "soon", "bit_rate": None},
        ),
        VIDEO,
    )
    assert probe == Probe(
        height=None, duration_seconds=None, bit_rate=None, video_codec=None
    )


def test_parse_rejects_non_json() -> None:
    with pytest.raises(ProbeError):
        _parse(b"not json", VIDEO)
    with pytest.raises(ProbeError):
        _parse(b"[1, 2]", VIDEO)


async def test_probe_without_binary_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(ffprobe, "find_ffprobe", lambda: None)
    assert await probe_video(VIDEO) is None


# ---- against the real binary --------------------------------------------
#
# The parser tests above pin our reading of the JSON; these pin the other
# side of the contract — that the ffmpeg/ffprobe actually installed accepts
# our fixed argv and answers with the fields the decision engine relies on.
# The CI binaries job runs them with REELOOM_TEST_REQUIRE_BINARIES=1, where
# a missing toolchain fails instead of skipping.

needs_ffmpeg = pytest.mark.binaries("ffmpeg")


def render_sample(path: Path, *, height: int = 240) -> None:
    """Encode a real one-second test video with the installed ffmpeg."""

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
        timeout=60,
    )


@needs_ffmpeg
@pytest.mark.parametrize("container", ["mkv", "mp4"])
async def test_real_ffprobe_answers_the_contract(
    tmp_path: Path, container: str
) -> None:
    video = tmp_path / f"probe.{container}"
    render_sample(video)

    probe = await probe_video(video)

    assert probe is not None
    assert probe.height == 240
    assert probe.video_codec == "mpeg4"
    assert probe.duration_seconds is not None
    assert 0.5 < probe.duration_seconds < 2.0
    assert probe.bit_rate is not None and probe.bit_rate > 0


@needs_ffmpeg
async def test_real_ffprobe_ranks_resolutions(tmp_path: Path) -> None:
    """The exact comparison the decision engine makes must hold end to end."""

    low = tmp_path / "low.mkv"
    high = tmp_path / "high.mkv"
    render_sample(low, height=240)
    render_sample(high, height=480)

    low_probe = await probe_video(low)
    high_probe = await probe_video(high)

    assert low_probe is not None and high_probe is not None
    assert low_probe.height is not None and high_probe.height is not None
    assert high_probe.height > low_probe.height


@needs_ffmpeg
async def test_real_ffprobe_rejects_a_non_video(tmp_path: Path) -> None:
    junk = tmp_path / "not-a-video.mkv"
    junk.write_bytes(b"this is not a video at all")

    with pytest.raises(ProbeError):
        await probe_video(junk)
