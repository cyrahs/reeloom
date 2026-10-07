""".torrent files reduced to the magnet CloudDrive can take.

CloudDrive offline tasks accept links only, so a torrent file handed to the
qBittorrent-compatible API is turned into a magnet before submission: the v1
info hash (SHA-1 of the bencoded ``info`` dictionary, byte for byte as it
appears in the file), the display name and the trackers. A private torrent is
refused — its trackers would see cloud servers instead of the account holder.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import quote

from reeloom.models import ReeloomError

#: Nothing a download client legitimately hands over is anywhere near this.
MAX_TORRENT_BYTES = 10 * 1024 * 1024
_MAX_DEPTH = 64
_MAX_TRACKERS = 50


class TorrentError(ReeloomError):
    pass


@dataclass(frozen=True, slots=True)
class TorrentMagnet:
    info_hash: str
    """Upper-case hex v1 hash, the same form ``magnet.extract_info_hash`` returns."""
    magnet: str
    private: bool


def magnet_from_torrent(data: bytes) -> TorrentMagnet:
    """Build a magnet from a .torrent file, or raise ``TorrentError``."""

    if len(data) > MAX_TORRENT_BYTES:
        raise TorrentError("torrent_too_large")
    decoder = _Decoder(data)
    root = decoder.decode()
    if decoder.position != len(data):
        raise TorrentError("torrent_trailing_data")
    if not isinstance(root, dict):
        raise TorrentError("torrent_not_a_dictionary")
    info = root.get(b"info")
    span = decoder.info_span
    if not isinstance(info, dict) or span is None:
        raise TorrentError("torrent_without_info")
    if b"pieces" not in info:
        # A v2-only torrent has no SHA-1 hash for CloudDrive to report.
        raise TorrentError("torrent_not_v1")

    info_hash = hashlib.sha1(data[span[0] : span[1]]).hexdigest().upper()
    private = info.get(b"private") == 1
    parts = [f"magnet:?xt=urn:btih:{info_hash}"]
    name = info.get(b"name.utf-8", info.get(b"name"))
    if isinstance(name, bytes) and name:
        parts.append("dn=" + quote(name.decode("utf-8", "replace"), safe=""))
    for tracker in _trackers(root):
        parts.append("tr=" + quote(tracker, safe=""))
    return TorrentMagnet(info_hash=info_hash, magnet="&".join(parts), private=private)


def _trackers(root: dict) -> list[str]:
    found: list[str] = []
    candidates: list[object] = [root.get(b"announce")]
    tiers = root.get(b"announce-list")
    if isinstance(tiers, list):
        for tier in tiers:
            if isinstance(tier, list):
                candidates.extend(tier)
    for candidate in candidates:
        if not isinstance(candidate, bytes):
            continue
        url = candidate.decode("utf-8", "replace").strip()
        if url and url not in found:
            found.append(url)
        if len(found) >= _MAX_TRACKERS:
            break
    return found


class _Decoder:
    """Minimal strict bencode reader that remembers where the top-level
    ``info`` value starts and ends, so the hash covers the original bytes."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.position = 0
        self.info_span: tuple[int, int] | None = None

    def decode(self, depth: int = 0) -> object:
        if depth > _MAX_DEPTH:
            raise TorrentError("torrent_too_deep")
        if self.position >= len(self._data):
            raise TorrentError("torrent_truncated")
        lead = self._data[self.position : self.position + 1]
        if lead == b"i":
            return self._integer()
        if lead == b"l":
            self.position += 1
            items: list[object] = []
            while self._peek() != b"e":
                items.append(self.decode(depth + 1))
            self.position += 1
            return items
        if lead == b"d":
            self.position += 1
            result: dict[bytes, object] = {}
            while self._peek() != b"e":
                key = self._string()
                start = self.position
                value = self.decode(depth + 1)
                if depth == 0 and key == b"info":
                    self.info_span = (start, self.position)
                result[key] = value
            self.position += 1
            return result
        if lead.isdigit():
            return self._string()
        raise TorrentError("torrent_malformed")

    def _peek(self) -> bytes:
        if self.position >= len(self._data):
            raise TorrentError("torrent_truncated")
        return self._data[self.position : self.position + 1]

    def _integer(self) -> int:
        end = self._data.find(b"e", self.position)
        if end == -1:
            raise TorrentError("torrent_truncated")
        try:
            value = int(self._data[self.position + 1 : end])
        except ValueError:
            raise TorrentError("torrent_malformed") from None
        self.position = end + 1
        return value

    def _string(self) -> bytes:
        colon = self._data.find(b":", self.position)
        if colon == -1:
            raise TorrentError("torrent_truncated")
        length_text = self._data[self.position : colon]
        if not length_text.isdigit():
            raise TorrentError("torrent_malformed")
        length = int(length_text)
        start = colon + 1
        end = start + length
        if end > len(self._data):
            raise TorrentError("torrent_truncated")
        self.position = end
        return self._data[start:end]
