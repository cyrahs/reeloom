"""magnet_from_torrent: bencode in, trackable magnet out."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest

from reeloom.magnet import extract_info_hash
from reeloom.torrent import TorrentError, magnet_from_torrent


def bencode(value: object) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items = sorted(
            (key.encode() if isinstance(key, str) else key, item)
            for key, item in value.items()
        )
        return (
            b"d" + b"".join(bencode(key) + bencode(item) for key, item in items) + b"e"
        )
    raise TypeError(value)


INFO = {
    "name": "Movie (2024) 1080p",
    "piece length": 262144,
    "pieces": b"\x01" * 20,
    "length": 1234,
}


def test_hash_covers_the_info_dictionary_bytes() -> None:
    data = bencode(
        {
            "announce": "udp://tracker.example:80/announce",
            "announce-list": [
                ["udp://tracker.example:80/announce"],
                ["http://second.example/announce"],
            ],
            "info": INFO,
        }
    )
    result = magnet_from_torrent(data)

    expected = hashlib.sha1(bencode(INFO)).hexdigest().upper()
    assert result.info_hash == expected
    assert result.private is False
    # The magnet tracks back to the same hash the poll joins on.
    assert extract_info_hash(result.magnet) == expected
    query = parse_qs(urlsplit(result.magnet).query)
    assert query["dn"] == ["Movie (2024) 1080p"]
    assert query["tr"] == [
        "udp://tracker.example:80/announce",
        "http://second.example/announce",
    ]


def test_private_torrents_are_flagged() -> None:
    data = bencode({"info": {**INFO, "private": 1}})
    assert magnet_from_torrent(data).private is True


def test_v2_only_torrents_are_refused() -> None:
    v2 = {"name": "x", "piece length": 16384, "meta version": 2, "file tree": {}}
    with pytest.raises(TorrentError) as info:
        magnet_from_torrent(bencode({"info": v2}))
    assert info.value.code == "torrent_not_v1"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "torrent_truncated"),
        (b"d4:info", "torrent_truncated"),
        (b"x", "torrent_malformed"),
        (bencode({"info": INFO}) + b"junk", "torrent_trailing_data"),
        (bencode(["not", "a", "dict"]), "torrent_not_a_dictionary"),
        (bencode({"announce": "x"}), "torrent_without_info"),
        (b"d4:infoi1ee", "torrent_without_info"),
        (b"999:short", "torrent_truncated"),
        (b"ixe", "torrent_malformed"),
    ],
)
def test_malformed_input_is_refused(data: bytes, code: str) -> None:
    with pytest.raises(TorrentError) as info:
        magnet_from_torrent(data)
    assert info.value.code == code


def test_nesting_is_bounded() -> None:
    with pytest.raises(TorrentError) as info:
        magnet_from_torrent(b"l" * 100 + b"e" * 100)
    assert info.value.code == "torrent_too_deep"
