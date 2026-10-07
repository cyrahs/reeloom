"""qBittorrent-compatible Web API over the magnet downloads.

Radarr, Sonarr and Prowlarr only talk to real download clients. This router
answers the slice of qBittorrent's Web API v2 they call, backed by
``DownloadService``: a torrent added here becomes a CloudDrive offline task,
tracked and concluded exactly like one submitted from the 下载 page, so the
same rows, poll, stall alerts and retry/delete buttons cover both.

- A category is a folder directly under the configured save root; the
  categories listing is that folder's subdirectories. Only rows whose
  download dir is the save root or one of those folders are visible here,
  so downloads started from the 下载 page never reach a client.
- A finished download reports ``pausedUP`` with its ratio limit already
  reached. That is what lets the client move the files on import — a
  cloud-side rename on the CloudDrive mount — instead of copying them back
  through FUSE.
- Deleting an unfinished download drops its CloudDrive task (with its data
  when the client asks). Deleting a concluded one only forgets the row:
  CloudDrive is never asked to delete a completed task's data, because the
  files may already be in the client's library.
- Nothing is fetched by URL. Magnets and uploaded .torrent files only;
  private torrents are refused.

Clients authenticate with the password stored in settings: the login cookie
qBittorrent issues, or the same password as a Bearer token or Basic auth.
The username is not checked.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import posixpath
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from reeloom.adapters.clouddrive import (
    CloudDriveError,
    validate_api_path,
    validate_path_segment,
)
from reeloom.magnet import extract_info_hash
from reeloom.models import DownloadState, MagnetDownload, ReeloomError
from reeloom.scanner import RESERVED_NAMES
from reeloom.server.composition import QbittorrentConfig
from reeloom.torrent import MAX_TORRENT_BYTES, TorrentError, magnet_from_torrent

_LOGGER = logging.getLogger(__name__)

#: What we claim to be. 2.9.x predates the stopped/stoppedUP rename, so
#: clients send ``paused`` and accept ``pausedUP``; >= 2.6.1 makes them
#: trust ``content_path`` for the import location.
WEB_API_VERSION = "2.9.3"
APP_VERSION = "v4.6.7"

SESSION_COOKIE = "SID"
SESSION_TTL_SECONDS = 24 * 3600
MAX_SESSIONS = 64

#: qBittorrent's "unknown" ETA; clients treat it as no estimate.
UNKNOWN_ETA = 8640000

_STATES = {
    DownloadState.SUBMITTED: "metaDL",
    DownloadState.DOWNLOADING: "downloading",
    DownloadState.MOVING: "moving",
    DownloadState.COMPLETED: "pausedUP",
    DownloadState.FAILED: "error",
    DownloadState.LOST: "error",
    DownloadState.STALLED: "stalledDL",
}

_NO_OP_ACTIONS = (
    "setShareLimits",
    "setForceStart",
    "setCategory",
    "addTags",
    "removeTags",
    "createTags",
    "deleteTags",
    "pause",
    "resume",
    "stop",
    "start",
    "recheck",
    "reannounce",
    "setSuperSeeding",
    "setAutoManagement",
    "toggleSequentialDownload",
    "toggleFirstLastPiecePrio",
    "setDownloadLimit",
    "setUploadLimit",
    "editCategory",
    "removeCategories",
)

_PRIORITY_ACTIONS = ("topPrio", "bottomPrio", "increasePrio", "decreasePrio")


class Sessions:
    """Login cookies, in memory: a restart only makes clients log in again.

    Each session remembers which password opened it, so changing the
    password in settings closes every session the old one opened.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[str, tuple[str, float]] = {}

    def open(self, password: str) -> str:
        now = self._clock()
        self._items = {sid: item for sid, item in self._items.items() if item[1] > now}
        while len(self._items) >= MAX_SESSIONS:
            oldest = min(self._items, key=lambda sid: self._items[sid][1])
            del self._items[oldest]
        sid = secrets.token_urlsafe(24)
        self._items[sid] = (_fingerprint(password), now + SESSION_TTL_SECONDS)
        return sid

    def valid(self, sid: str, password: str) -> bool:
        item = self._items.get(sid)
        if item is None or item[1] <= self._clock():
            return False
        return secrets.compare_digest(item[0], _fingerprint(password))

    def close(self, sid: str) -> None:
        self._items.pop(sid, None)


def _fingerprint(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def create_qbittorrent_router(
    *,
    database: Any,
    downloads: Any,
    clients: Any,
    wake: Callable[[], None] = lambda: None,
    sessions: Sessions | None = None,
) -> APIRouter:
    sessions = sessions or Sessions()
    router = APIRouter(prefix="/api/v2", include_in_schema=False)

    async def configured() -> QbittorrentConfig | None:
        try:
            return await clients.qbittorrent()
        except ReeloomError:
            return None

    async def require(request: Request) -> QbittorrentConfig:
        config = await configured()
        if config is None or not _authorized(request, config, sessions):
            # qBittorrent answers 403 to anything unauthenticated; clients
            # read that as "log in", not as a missing API.
            raise HTTPException(status_code=403, detail="Forbidden")
        return config

    authorized = Depends(require)

    # ---- auth ---------------------------------------------------------

    @router.post("/auth/login")
    async def login(request: Request):
        form = await request.form()
        password = str(form.get("password", ""))
        config = await configured()
        if (
            config is None
            or not password
            or not secrets.compare_digest(password.encode(), config.password.encode())
        ):
            return PlainTextResponse("Fails.")
        response = PlainTextResponse("Ok.")
        response.set_cookie(
            SESSION_COOKIE, sessions.open(config.password), httponly=True, path="/"
        )
        return response

    @router.post("/auth/logout")
    async def logout(request: Request):
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            sessions.close(sid)
        return PlainTextResponse("")

    # ---- application --------------------------------------------------

    @router.get("/app/webapiVersion")
    async def web_api_version(config: QbittorrentConfig = authorized):
        return PlainTextResponse(WEB_API_VERSION)

    @router.get("/app/version")
    async def app_version(config: QbittorrentConfig = authorized):
        return PlainTextResponse(APP_VERSION)

    @router.get("/app/defaultSavePath")
    async def default_save_path(config: QbittorrentConfig = authorized):
        return PlainTextResponse(config.save_root)

    @router.get("/app/preferences")
    async def preferences(config: QbittorrentConfig = authorized):
        # A global ratio limit of 0 that pauses (never removes) is what
        # makes every finished download count as done seeding; no queueing,
        # so priority calls are answered the way qBittorrent does then.
        return {
            "save_path": config.save_root,
            "max_ratio_enabled": True,
            "max_ratio": 0,
            "max_ratio_act": 0,
            "max_seeding_time_enabled": False,
            "max_seeding_time": -1,
            "max_inactive_seeding_time_enabled": False,
            "max_inactive_seeding_time": -1,
            "queueing_enabled": False,
            "dht": True,
        }

    # ---- categories ---------------------------------------------------

    @router.get("/torrents/categories")
    async def categories(config: QbittorrentConfig = authorized):
        cloud = await _cloud(clients)
        try:
            # Clients ask on every status check; CloudDrive's cached listing
            # is fresh enough and spares the cloud an API call each time.
            entries = await cloud.list_directory(config.save_root, force_refresh=False)
        except CloudDriveError as error:
            if error.code == "clouddrive_path_not_found":
                return {}
            raise _unavailable(error)
        return {
            str(entry["name"]): {
                "name": str(entry["name"]),
                "savePath": posixpath.join(config.save_root, str(entry["name"])),
            }
            for entry in entries
            if entry["is_directory"] and _is_category_name(str(entry["name"]))
        }

    @router.post("/torrents/createCategory")
    async def create_category(request: Request, config: QbittorrentConfig = authorized):
        form = await request.form()
        name = str(form.get("category", "")).strip()
        if not _is_category_name(name):
            return PlainTextResponse("Category name is invalid", status_code=400)
        cloud = await _cloud(clients)
        try:
            await _ensure_category(cloud, config.save_root, name)
        except CloudDriveError as error:
            raise _unavailable(error)
        return PlainTextResponse("")

    # ---- torrents -----------------------------------------------------

    @router.get("/torrents/info")
    async def info(
        category: str | None = None,
        hashes: str | None = None,
        config: QbittorrentConfig = authorized,
    ):
        wanted = _hash_filter(hashes)
        items = []
        for download, item_category in await _visible(database, config):
            if category is not None and item_category != category:
                continue
            if wanted is not None and download.info_hash not in wanted:
                continue
            items.append(_torrent_json(download, item_category))
        return JSONResponse(items)

    @router.get("/torrents/properties")
    async def properties(hash: str = "", config: QbittorrentConfig = authorized):
        found = await _find(database, config, hash)
        if found is None:
            return PlainTextResponse("Not Found", status_code=404)
        download, item_category = found
        torrent = _torrent_json(download, item_category)
        return {
            "hash": torrent["hash"],
            "name": torrent["name"],
            "save_path": torrent["save_path"],
            "total_size": torrent["size"],
            "addition_date": torrent["added_on"],
            "completion_date": torrent["completion_on"],
            "seeding_time": 0,
            "share_ratio": 0,
            "eta": torrent["eta"],
        }

    @router.get("/torrents/files")
    async def files(hash: str = "", config: QbittorrentConfig = authorized):
        found = await _find(database, config, hash)
        if found is None:
            return PlainTextResponse("Not Found", status_code=404)
        download, _ = found
        if download.state is not DownloadState.COMPLETED or not download.final_path:
            name = download.name or _display_name(download)
            return [
                {
                    "index": 0,
                    "name": name,
                    "size": download.size_bytes or 0,
                    "progress": (download.progress or 0) / 100,
                }
            ]
        cloud = await _cloud(clients)
        top = posixpath.basename(download.final_path)
        try:
            entries = await cloud.list_directory(download.final_path)
        except CloudDriveError as error:
            if error.code == "clouddrive_path_not_found":
                return []
            raise _unavailable(error)
        return [
            {
                "index": index,
                "name": f"{top}/{entry['name']}",
                "size": int(entry["size"]),
                "progress": 1,
            }
            for index, entry in enumerate(
                entry for entry in entries if not entry["is_directory"]
            )
        ]

    @router.post("/torrents/add")
    async def add(request: Request, config: QbittorrentConfig = authorized):
        form = await request.form()
        category = str(form.get("category", "") or "").strip()
        if category and not _is_category_name(category):
            return PlainTextResponse("Fails.")

        magnets: list[str] = []
        for line in str(form.get("urls", "") or "").splitlines():
            url = line.strip()
            if not url:
                continue
            if not url.lower().startswith("magnet:"):
                # No outbound fetches: the client downloads a .torrent itself
                # and uploads it instead.
                _LOGGER.info("qbittorrent add refused a non-magnet url")
                continue
            magnets.append(url)
        for upload in form.getlist("torrents"):
            if isinstance(upload, str):
                continue
            data = await upload.read(MAX_TORRENT_BYTES + 1)
            try:
                torrent = magnet_from_torrent(data)
            except TorrentError as error:
                _LOGGER.info("qbittorrent add refused a torrent: %s", error.code)
                continue
            if torrent.private:
                _LOGGER.info(
                    "qbittorrent add refused private torrent hash=%s",
                    torrent.info_hash,
                )
                continue
            magnets.append(torrent.magnet)
        if not magnets:
            return PlainTextResponse("Fails.")

        directory = (
            posixpath.join(config.save_root, category) if category else config.save_root
        )
        cloud = await _cloud(clients)
        try:
            validate_api_path(directory, allow_root=False)
            if category:
                await _ensure_category(cloud, config.save_root, category)
        except CloudDriveError as error:
            _LOGGER.warning("qbittorrent add: category dir failed: %s", error.code)
            return PlainTextResponse("Fails.")

        added = 0
        for magnet in magnets:
            if await _submit(database, downloads, magnet, directory):
                added += 1
        if added:
            wake()
        return PlainTextResponse("Ok." if added else "Fails.")

    @router.post("/torrents/delete")
    async def delete(request: Request, config: QbittorrentConfig = authorized):
        form = await request.form()
        raw = str(form.get("hashes", "") or "")
        delete_files = str(form.get("deleteFiles", "false")).lower() == "true"
        visible = await _visible(database, config)
        if raw.strip().lower() == "all":
            targets = [download for download, _ in visible]
        else:
            wanted = _hash_filter(raw) or set()
            targets = [
                download for download, _ in visible if download.info_hash in wanted
            ]
        for download in targets:
            await _forget(database, downloads, download, delete_files=delete_files)
        return PlainTextResponse("")

    async def no_op(config: QbittorrentConfig = authorized):
        return PlainTextResponse("")

    for action in _NO_OP_ACTIONS:
        router.add_api_route(f"/torrents/{action}", no_op, methods=["POST"])

    async def priority(config: QbittorrentConfig = authorized):
        return PlainTextResponse("Torrent queueing must be enabled", status_code=409)

    for action in _PRIORITY_ACTIONS:
        router.add_api_route(f"/torrents/{action}", priority, methods=["POST"])

    return router


# ---- helpers -------------------------------------------------------------


def _authorized(
    request: Request, config: QbittorrentConfig, sessions: Sessions
) -> bool:
    sid = request.cookies.get(SESSION_COOKIE)
    if sid and sessions.valid(sid, config.password):
        return True
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if scheme.lower() == "bearer" and value:
        return secrets.compare_digest(value.encode(), config.password.encode())
    if scheme.lower() == "basic" and value:
        try:
            decoded = base64.b64decode(value, validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return False
        _, _, password = decoded.partition(":")
        return bool(password) and secrets.compare_digest(
            password.encode(), config.password.encode()
        )
    return False


def _is_category_name(name: str) -> bool:
    if not name or name.startswith(".") or name.casefold() in RESERVED_NAMES:
        return False
    try:
        validate_path_segment(name)
    except CloudDriveError:
        return False
    return True


def _category_of(download: MagnetDownload, save_root: str) -> str | None:
    """The category a row files under, or None when it is not ours."""

    if download.download_dir == save_root:
        return ""
    prefix = save_root.rstrip("/") + "/"
    if not download.download_dir.startswith(prefix):
        return None
    rest = download.download_dir[len(prefix) :]
    return rest if _is_category_name(rest) else None


async def _visible(
    database: Any, config: QbittorrentConfig
) -> list[tuple[MagnetDownload, str]]:
    rows = await database.magnet_downloads_under(config.save_root)
    visible = []
    for download in rows:
        if download.state is DownloadState.REMOVED:
            continue
        category = _category_of(download, config.save_root)
        if category is not None:
            visible.append((download, category))
    return visible


async def _find(
    database: Any, config: QbittorrentConfig, raw_hash: str
) -> tuple[MagnetDownload, str] | None:
    wanted = raw_hash.strip().upper()
    matches = [
        item for item in await _visible(database, config) if item[0].info_hash == wanted
    ]
    # A hash can have older concluded rows; the newest one is the torrent.
    return matches[-1] if matches else None


def _hash_filter(raw: str | None) -> set[str] | None:
    if raw is None or not raw.strip() or raw.strip().lower() == "all":
        return None
    return {part.strip().upper() for part in raw.split("|") if part.strip()}


def _display_name(download: MagnetDownload) -> str:
    if download.name:
        return download.name
    query = parse_qs(urlsplit(download.magnet).query)
    names = query.get("dn")
    if names and names[0].strip():
        return names[0].strip()
    return download.info_hash.lower()


def _timestamp(value: Any) -> int:
    return int(value.timestamp()) if value is not None else 0


def _torrent_json(download: MagnetDownload, category: str) -> dict[str, Any]:
    name = _display_name(download)
    completed = download.state is DownloadState.COMPLETED
    size = download.size_bytes or 0
    progress = 1.0 if completed else min((download.progress or 0) / 100, 1.0)
    if completed:
        content_path = download.final_path or posixpath.join(
            download.download_dir, name
        )
    else:
        content_path = posixpath.join(download.download_dir, "in_progress", name)
    return {
        "hash": download.info_hash.lower(),
        "infohash_v1": download.info_hash.lower(),
        "name": name,
        "size": size,
        "total_size": size,
        "progress": progress,
        "amount_left": int(size * (1 - progress)),
        "dlspeed": 0,
        "upspeed": 0,
        "eta": 0 if completed else UNKNOWN_ETA,
        "state": _STATES.get(download.state, "error"),
        "category": category,
        "tags": "",
        "save_path": download.download_dir,
        "content_path": content_path,
        "ratio": 0,
        "ratio_limit": -2,
        "seeding_time": 0,
        "seeding_time_limit": -2,
        "inactive_seeding_time_limit": -2,
        "num_seeds": 0,
        "num_leechs": 0,
        "added_on": _timestamp(download.created_at),
        "completion_on": _timestamp(download.updated_at) if completed else -1,
        "last_activity": _timestamp(download.updated_at),
        "magnet_uri": download.magnet,
        "error": download.error or "",
    }


async def _submit(database: Any, downloads: Any, magnet: str, directory: str) -> bool:
    info_hash = extract_info_hash(magnet)
    if info_hash is None:
        return False
    try:
        await downloads.submit(magnet, directory)
    except ReeloomError as error:
        if error.code == "duplicate_download":
            # Re-adding what this directory is already downloading is a
            # no-op success, as it is for a client retrying a grab.
            live = await database.live_magnet_downloads()
            return any(
                item.info_hash == info_hash and item.download_dir == directory
                for item in live
            )
        _LOGGER.warning("qbittorrent add failed hash=%s: %s", info_hash, error.code)
        return False
    return True


async def _forget(
    database: Any, downloads: Any, download: MagnetDownload, *, delete_files: bool
) -> None:
    if download.state.is_terminal:
        # Concluded: forget the row, never touch CloudDrive (see module doc).
        await database.delete_magnet_download(download.id)
        return
    if download.state is DownloadState.MOVING:
        return  # concludes on the next poll; the client will see it finish
    try:
        await downloads.remove(download.id, delete_files=delete_files)
    except ReeloomError as error:
        _LOGGER.warning(
            "qbittorrent delete failed download=%s: %s", download.id, error.code
        )


async def _ensure_category(cloud: Any, save_root: str, name: str) -> None:
    parent, leaf = posixpath.split(save_root)
    if parent and leaf:
        await cloud.ensure_directory(parent, leaf)
    await cloud.ensure_directory(save_root, name)


async def _cloud(clients: Any) -> Any:
    try:
        return await clients.clouddrive()
    except ReeloomError as error:
        raise _unavailable(error)


def _unavailable(error: ReeloomError) -> HTTPException:
    return HTTPException(status_code=503, detail=error.code)
