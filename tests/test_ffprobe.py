from __future__ import annotations

import json
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
