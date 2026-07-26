from __future__ import annotations

import json
import mimetypes
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from reeloom.server.errors import ServerError, ServerErrorCode

_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_INDEX_BYTES = 1024 * 1024
_MAX_ASSET_BYTES = 16 * 1024 * 1024
_HASH_ASSET = re.compile(
    r"^assets/[A-Za-z0-9_-]+-[A-Za-z0-9_-]{8,}\.(?:css|js)$"
)


@dataclass(frozen=True, slots=True)
class StaticAsset:
    content: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class StaticWebBundle:
    index: StaticAsset
    assets: dict[str, StaticAsset]

    @property
    def public_paths(self) -> frozenset[str]:
        return frozenset({"/", *self.assets})

    @classmethod
    def load(cls, root: Path) -> StaticWebBundle:
        root_fd = -1
        try:
            root_fd = os.open(
                root,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
            )
            manifest_bytes = _read_regular_at(
                root_fd,
                PurePosixPath("manifest.json"),
                limit=_MAX_MANIFEST_BYTES,
            )
            raw = json.loads(manifest_bytes)
            if not isinstance(raw, dict) or not raw:
                raise ValueError
            names: set[PurePosixPath] = set()
            entry = raw.get("index.html")
            if not isinstance(entry, dict) or entry.get("isEntry") is not True:
                raise ValueError
            for value in raw.values():
                if not isinstance(value, dict):
                    raise ValueError
                file_value = value.get("file")
                if isinstance(file_value, str):
                    names.add(_asset_path(file_value))
                for field in ("css", "assets"):
                    listed = value.get(field, [])
                    if not isinstance(listed, list) or not all(
                        isinstance(item, str) for item in listed
                    ):
                        raise ValueError
                    names.update(_asset_path(item) for item in listed)
            index_bytes = _read_regular_at(
                root_fd,
                PurePosixPath("index.html"),
                limit=_MAX_INDEX_BYTES,
            )
            assets: dict[str, StaticAsset] = {}
            for name in names:
                content = _read_regular_at(
                    root_fd,
                    name,
                    limit=_MAX_ASSET_BYTES,
                )
                media_type = (
                    mimetypes.guess_type(name.name)[0]
                    or "application/octet-stream"
                )
                assets[f"/{name.as_posix()}"] = StaticAsset(
                    content=content,
                    media_type=media_type,
                )
            return cls(
                index=StaticAsset(
                    content=index_bytes,
                    media_type="text/html",
                ),
                assets=assets,
            )
        except ServerError:
            raise
        except (
            json.JSONDecodeError,
            OSError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
        ):
            raise ServerError(ServerErrorCode.UNSAFE_STATE_ROOT) from None
        finally:
            if root_fd >= 0:
                os.close(root_fd)


def _asset_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(
            part in {"", ".", ".."}
            or part.startswith(".")
            or len(part.encode("utf-8")) > 255
            for part in path.parts
        )
        or len(value.encode("utf-8")) > 1024
        or _HASH_ASSET.fullmatch(value) is None
    ):
        raise ValueError
    return path


def _read_regular_at(
    root_fd: int,
    path: PurePosixPath,
    *,
    limit: int,
) -> bytes:
    directory_fds: list[int] = []
    current_fd = root_fd
    file_fd = -1
    try:
        for part in path.parts[:-1]:
            current_fd = os.open(
                part,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current_fd,
            )
            directory_fds.append(current_fd)
        file_fd = os.open(
            path.name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=current_fd,
        )
        metadata = os.fstat(file_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= limit
        ):
            raise ValueError
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(file_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        result = b"".join(chunks)
        after = os.fstat(file_fd)
        if (
            len(result) != metadata.st_size
            or after.st_dev != metadata.st_dev
            or after.st_ino != metadata.st_ino
            or after.st_size != metadata.st_size
            or after.st_mtime_ns != metadata.st_mtime_ns
            or after.st_ctime_ns != metadata.st_ctime_ns
        ):
            raise ValueError
        return result
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)
