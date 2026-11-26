"""HTTP API.

Single admin, same-origin UI, no sessions: one Bearer token guards every
route. The UI polls; there is no event stream to reconnect, buffer or replay.
"""

from __future__ import annotations

import logging
import posixpath
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from reeloom.adapters.clouddrive import validate_api_path
from reeloom.db import Database
from reeloom.models import (
    GrabState,
    MediaType,
    ReeloomError,
    Run,
    RunState,
    SubtitleVariant,
)
from reeloom.models import Subscription as SubscriptionModel
from reeloom.models import WatchConfig as WatchConfigModel
from reeloom.release import compile_pattern
from reeloom.server.worker import (
    DEFAULT_SUBTITLE_RECHECK_DAYS,
    RECHECK_GIVEN_UP,
    RECHECK_SEARCHING,
    RECHECK_WAITING,
    subtitle_recheck_status,
)

_LOGGER = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 2000


class ConfigInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    inbound_root: str = Field(min_length=1, max_length=1024)
    library_root: str = Field(min_length=1, max_length=1024)
    media_type: MediaType
    enabled: bool = True
    stability_seconds: int = Field(default=120, ge=0, le=86_400)
    acquire_subtitles: bool = False
    subtitle_variant: Literal["chs", "cht"] = "chs"
    notify: bool = True
    replace_enabled: bool = False
    replace_extra_dirs: list[Annotated[str, Field(min_length=1, max_length=1024)]] = (
        Field(default_factory=list, max_length=8)
    )
    replace_auto_ratio: float = Field(default=1.2, ge=1.0, le=10.0)
    cross_filesystem: bool = False


class ConfigPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    inbound_root: str | None = Field(default=None, min_length=1, max_length=1024)
    library_root: str | None = Field(default=None, min_length=1, max_length=1024)
    media_type: MediaType | None = None
    enabled: bool | None = None
    stability_seconds: int | None = Field(default=None, ge=0, le=86_400)
    acquire_subtitles: bool | None = None
    subtitle_variant: Literal["chs", "cht"] | None = None
    notify: bool | None = None
    replace_enabled: bool | None = None
    replace_extra_dirs: (
        list[Annotated[str, Field(min_length=1, max_length=1024)]] | None
    ) = Field(default=None, max_length=8)
    replace_auto_ratio: float | None = Field(default=None, ge=1.0, le=10.0)
    cross_filesystem: bool | None = None


class SettingsInput(BaseModel):
    tmdb_api_key: str | None = Field(default=None, max_length=200)
    llm_base_url: str | None = Field(default=None, max_length=500)
    llm_api_key: str | None = Field(default=None, max_length=500)
    llm_model: str | None = Field(default=None, max_length=200)
    # Empty string means "provider default": the parameter is not sent at all.
    llm_reasoning_effort: (
        Literal["", "minimal", "low", "medium", "high", "xhigh", "max"] | None
    ) = None
    telegram_bot_token: str | None = Field(default=None, max_length=200)
    telegram_chat_id: str | None = Field(default=None, max_length=64)
    telegram_pin_alerts: bool | None = None
    trash_retention_days: int | None = Field(default=None, ge=0, le=365)
    clouddrive_address: str | None = Field(default=None, max_length=200)
    clouddrive_api_token: str | None = Field(default=None, max_length=500)
    clouddrive_secure: bool | None = None
    download_stall_hours: int | None = Field(default=None, ge=1, le=720)
    # Days after a run is created during which a finished anime run still
    # short of subtitles gets one more ACG.RIP search per day; 0 turns it off.
    subtitle_recheck_days: int | None = Field(default=None, ge=0, le=365)
    # The qBittorrent-compatible API (/api/v2) opens once both are set.
    qbittorrent_password: str | None = Field(
        default=None, min_length=8, max_length=200
    )
    qbittorrent_save_root: str | None = Field(default=None, max_length=1024)
    # Subscriptions: the Prowlarr that searches the indexer, and the real
    # qBittorrent the releases download into.
    prowlarr_url: str | None = Field(default=None, max_length=500)
    prowlarr_api_key: str | None = Field(default=None, max_length=200)
    prowlarr_indexer_ids: str | None = Field(default=None, max_length=200)
    pt_client_url: str | None = Field(default=None, max_length=500)
    pt_client_username: str | None = Field(default=None, max_length=200)
    pt_client_password: str | None = Field(default=None, max_length=500)
    subscription_interval_minutes: int | None = Field(default=None, ge=5, le=1440)


_Group = Annotated[str, Field(min_length=1, max_length=64)]


class SubscriptionInput(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    config_id: str = Field(min_length=1, max_length=64)
    tmdb_id: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=200)
    season: int | None = Field(default=None, ge=0, le=999)
    groups: list[_Group] = Field(default_factory=list, max_length=20)
    include: str = Field(default="", max_length=500)
    exclude: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=2000)
    enabled: bool = True
    backfill: bool = True
    """Grab releases already on the indexer; off: only ones published later."""


class SubscriptionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config_id: str | None = Field(default=None, min_length=1, max_length=64)
    tmdb_id: int | None = Field(default=None, ge=1)
    query: str | None = Field(default=None, min_length=1, max_length=200)
    season: int | None = Field(default=None, ge=0, le=999)
    clear_season: bool = False
    groups: list[_Group] | None = Field(default=None, max_length=20)
    include: str | None = Field(default=None, max_length=500)
    exclude: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


class MessageInput(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


class DownloadInput(BaseModel):
    magnet: str = Field(min_length=20, max_length=8192)
    directory: str = Field(min_length=2, max_length=1024)


class ReplaceResolveInput(BaseModel):
    action: Literal["replace", "discard_incoming", "keep_both"]


def create_app(
    *,
    database: Database,
    admin_token: str,
    worker=None,
    answerer=None,
    notifier=None,
    downloads=None,
    subscriptions=None,
    clients=None,
    static_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Reeloom", version="2.0.0", docs_url=None, redoc_url=None)

    async def authorize(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = f"Bearer {admin_token}"
        if authorization is None or not secrets.compare_digest(
            authorization, expected
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    api = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    async def load(run_id: str) -> Run:
        run = await database.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run_not_found")
        return run

    def nudge() -> None:
        if worker is not None:
            worker.wake()

    # ---- intake -------------------------------------------------------

    @api.get("/intake")
    async def intake():
        """Inbound folders the scanner is watching but has not turned into
        runs: settling ones with their remaining wait, empty ones, and stable
        ones it skipped. ``now`` lets the client turn ``remaining_seconds``
        (measured at ``scanned_at``) into a live countdown without trusting
        its own clock against the server's."""

        folders = worker.intake_status() if worker is not None else []
        return {
            "now": time.time(),
            "folders": [folder.to_json() for folder in folders],
        }

    # ---- runs ---------------------------------------------------------

    @api.get("/runs")
    async def list_runs(state: RunState | None = None, limit: int = 100):
        runs = await database.list_runs(
            states=[state] if state else None, limit=min(limit, 500)
        )
        return {"runs": [_run_summary(run) for run in runs]}

    @api.get("/runs/{run_id}")
    async def get_run(run_id: str):
        run = await load(run_id)
        return {
            **_run_summary(run),
            "snapshot": [item.to_json() for item in run.snapshot],
            "plan": run.plan.to_json() if run.plan else None,
            "executed_moves": [item.to_json() for item in run.executed_moves],
            "replace_decision": run.extra.get("replace"),
            "logs": await database.list_logs(run_id),
            "interactions": await database.list_interactions(run_id),
        }

    @api.post("/runs/{run_id}/ask")
    async def ask(run_id: str, payload: MessageInput):
        """Ask about a run. Read-only: it never changes state or the plan.

        The stored chat is the model's context: every earlier message of this
        run is replayed, so the conversation the user sees is the conversation
        the model has.
        """

        run = await load(run_id)
        if answerer is None:
            raise HTTPException(status_code=503, detail="model_not_configured")
        config = await database.get_config(run.config_id)
        if config is None:
            raise HTTPException(status_code=409, detail="config_deleted")
        history = await database.list_interactions(run_id)
        await database.add_interaction(run_id, "user", payload.message)
        answer = await answerer.answer(run, config, payload.message, history)
        await database.add_interaction(run_id, "agent", answer)
        return {"answer": answer}

    @api.post("/runs/{run_id}/revise")
    async def revise(run_id: str, payload: MessageInput):
        """Re-plan with feedback, undoing an executed layout first if needed.

        The message is stored as a ``revision`` interaction: it stays in the
        chat log, and re-identification replays the whole log, revisions as
        binding instructions and the rest as context.
        """

        run = await load(run_id)
        if run.state.is_active:
            raise HTTPException(status_code=409, detail="run_is_busy")
        await database.add_interaction(run_id, "revision", payload.message)
        await database.log(run_id, "revision requested")
        await database.set_state(run_id, RunState.IDENTIFYING)
        nudge()
        return {"state": RunState.IDENTIFYING.value}

    @api.post("/runs/{run_id}/replace")
    async def resolve_replace(run_id: str, payload: ReplaceResolveInput):
        """Answer a parked replacement confirmation.

        The choice is stored next to the decision and the run re-enters
        COMPARING, which recomputes against the current filesystem and only
        honors the choice if the facts the user saw still hold.
        """

        run = await load(run_id)
        if run.state.is_active:
            raise HTTPException(status_code=409, detail="run_is_busy")
        if run.state is not RunState.NEEDS_ATTENTION or (
            (run.error or {}).get("code") != "replace_confirmation"
        ):
            raise HTTPException(
                status_code=409, detail="not_awaiting_replace_confirmation"
            )
        stored = dict(run.extra.get("replace") or {})
        stored["resolution"] = payload.action
        await database.set_extra(run_id, {**run.extra, "replace": stored})
        await database.log(
            run_id, f"replacement resolved: {payload.action}"
        )
        await database.set_state(run_id, RunState.COMPARING)
        nudge()
        return {"state": RunState.COMPARING.value}

    @api.post("/runs/{run_id}/retry")
    async def retry(run_id: str):
        run = await load(run_id)
        if run.state.is_active:
            raise HTTPException(status_code=409, detail="run_is_busy")
        # Executed runs resume from their ledger; unexecuted ones re-identify.
        state = RunState.EXECUTING if run.plan and run.executed_moves else RunState.PENDING
        await database.set_state(run_id, state)
        nudge()
        return {"state": state.value}

    @api.post("/runs/{run_id}/discard")
    async def discard(run_id: str):
        run = await load(run_id)
        if run.state.is_active:
            raise HTTPException(status_code=409, detail="run_is_busy")
        await database.set_state(run_id, RunState.DISCARDING)
        nudge()
        return {"state": RunState.DISCARDING.value}

    @api.delete("/runs/{run_id}")
    async def delete_run(run_id: str):
        run = await load(run_id)
        if run.state.is_active:
            raise HTTPException(status_code=409, detail="run_is_busy")
        await database.delete_run(run_id)
        return {"deleted": True}

    # ---- configuration ------------------------------------------------

    @api.get("/configs")
    async def list_configs():
        return {
            "configs": [
                _config_json(config) for config in await database.list_configs()
            ]
        }

    @api.post("/configs", status_code=201)
    async def create_config(payload: ConfigInput):
        _check_roots(payload.inbound_root, payload.library_root)
        _check_extra_dirs(
            payload.replace_extra_dirs, payload.inbound_root, payload.library_root
        )
        config = WatchConfigModel(
            id=str(uuid.uuid4()),
            name=payload.name,
            inbound_root=payload.inbound_root,
            library_root=payload.library_root,
            media_type=payload.media_type,
            enabled=payload.enabled,
            stability_seconds=payload.stability_seconds,
            acquire_subtitles=payload.acquire_subtitles,
            subtitle_variant=SubtitleVariant(payload.subtitle_variant),
            notify=payload.notify,
            replace_enabled=payload.replace_enabled,
            replace_extra_dirs=tuple(payload.replace_extra_dirs),
            replace_auto_ratio=payload.replace_auto_ratio,
            cross_filesystem=payload.cross_filesystem,
        )
        await database.create_config(config)
        nudge()
        return _config_json(config)

    @api.patch("/configs/{config_id}")
    async def update_config(config_id: str, payload: ConfigPatch):
        stored = await database.get_config(config_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="config_not_found")
        values = payload.model_dump(exclude_none=True)
        if "media_type" in values:
            values["media_type"] = values["media_type"].value
        if "inbound_root" in values or "library_root" in values:
            _check_roots(values.get("inbound_root"), values.get("library_root"))
        # Validate extra dirs against the roots the config will actually have.
        effective_dirs = values.get(
            "replace_extra_dirs", list(stored.replace_extra_dirs)
        )
        if effective_dirs:
            _check_extra_dirs(
                effective_dirs,
                values.get("inbound_root", stored.inbound_root),
                values.get("library_root", stored.library_root),
            )
        await database.update_config(config_id, values)
        nudge()
        return _config_json(await database.get_config(config_id))

    @api.delete("/configs/{config_id}")
    async def delete_config(config_id: str):
        await database.delete_config(config_id)
        return {"deleted": True}

    # ---- filesystem ---------------------------------------------------

    @api.get("/fs/dirs")
    async def list_dirs(path: str = "/"):
        """List server-side subdirectories, for the config path pickers."""

        base = Path(path)
        if not base.is_absolute():
            raise HTTPException(status_code=422, detail="path_must_be_absolute")
        base = base.resolve()
        if not base.is_dir():
            raise HTTPException(status_code=404, detail="not_a_directory")
        try:
            dirs = sorted(
                (
                    entry.name
                    for entry in base.iterdir()
                    if entry.is_dir() and not entry.name.startswith(".")
                ),
                key=str.casefold,
            )
        except PermissionError:
            raise HTTPException(status_code=403, detail="permission_denied")
        parent = None if base.parent == base else str(base.parent)
        return {"path": str(base), "parent": parent, "dirs": dirs}

    # ---- settings -----------------------------------------------------

    @api.get("/settings")
    async def get_settings():
        stored = await database.get_settings()
        # Secrets are reported as present or absent, never echoed back.
        return {
            "llm_base_url": stored.get("llm_base_url", ""),
            "llm_model": stored.get("llm_model", ""),
            "llm_reasoning_effort": stored.get("llm_reasoning_effort", ""),
            "telegram_chat_id": stored.get("telegram_chat_id", ""),
            "telegram_pin_alerts": bool(stored.get("telegram_pin_alerts", True)),
            "trash_retention_days": stored.get("trash_retention_days", 3),
            "clouddrive_address": stored.get("clouddrive_address", ""),
            "clouddrive_secure": bool(stored.get("clouddrive_secure", True)),
            "download_stall_hours": stored.get("download_stall_hours", 24),
            "subtitle_recheck_days": stored.get("subtitle_recheck_days", 30),
            "qbittorrent_save_root": stored.get("qbittorrent_save_root", ""),
            "prowlarr_url": stored.get("prowlarr_url", ""),
            "prowlarr_indexer_ids": stored.get("prowlarr_indexer_ids", ""),
            "pt_client_url": stored.get("pt_client_url", ""),
            "pt_client_username": stored.get("pt_client_username", ""),
            "subscription_interval_minutes": stored.get(
                "subscription_interval_minutes", 30
            ),
            "prowlarr_api_key_set": bool(stored.get("prowlarr_api_key")),
            "pt_client_password_set": bool(stored.get("pt_client_password")),
            "tmdb_api_key_set": bool(stored.get("tmdb_api_key")),
            "llm_api_key_set": bool(stored.get("llm_api_key")),
            "telegram_bot_token_set": bool(stored.get("telegram_bot_token")),
            "clouddrive_api_token_set": bool(stored.get("clouddrive_api_token")),
            "qbittorrent_password_set": bool(stored.get("qbittorrent_password")),
        }

    @api.put("/settings")
    async def put_settings(payload: SettingsInput):
        values = payload.model_dump(exclude_none=True)
        if "qbittorrent_save_root" in values:
            root = values["qbittorrent_save_root"].strip()
            if len(root) > 1:
                root = root.rstrip("/")
            try:
                validate_api_path(root, allow_root=False)
            except ReeloomError as error:
                raise HTTPException(status_code=422, detail=error.code)
            values["qbittorrent_save_root"] = root
        for key in ("prowlarr_url", "pt_client_url"):
            if key in values:
                values[key] = values[key].strip().rstrip("/")
                if values[key] and not values[key].startswith(
                    ("http://", "https://")
                ):
                    raise HTTPException(status_code=422, detail=f"{key}_invalid")
        await database.update_settings(values)
        nudge()
        return {"updated": True}

    @api.post("/settings/test-llm")
    async def test_llm():
        """Fire a minimal completion at the configured model so the admin
        can check the saved credentials from the settings page. Expected
        failures come back as ``ok: false`` rather than an error status."""

        if answerer is None:
            return {"ok": False, "error": "model_not_configured"}
        try:
            reply = await answerer.ping()
        except ReeloomError as error:
            return {"ok": False, "error": str(error)[:500]}
        return {"ok": True, "reply": reply}

    @api.post("/settings/test-telegram")
    async def test_telegram():
        """Send two test notifications — one plain, one needs-attention —
        so the admin can check the saved Telegram credentials and pinning."""

        if notifier is None:
            return {"ok": False, "error": "telegram_not_configured"}
        error = await notifier.send_test()
        return {"ok": True} if error is None else {"ok": False, "error": error}

    @api.post("/settings/test-clouddrive")
    async def test_clouddrive():
        """Ping the configured CloudDrive2 server and list its root, so the
        admin can check the saved address and token from the settings page."""

        if clients is None:
            return {"ok": False, "error": "clouddrive_not_configured"}
        try:
            cloud = await clients.clouddrive()
            await cloud.check()
        except ReeloomError as error:
            return {"ok": False, "error": error.code}
        return {"ok": True}

    @api.post("/settings/test-prowlarr")
    async def test_prowlarr():
        """List Prowlarr's indexers, so the admin can check the address and
        key and find the indexer ids to search."""

        if clients is None:
            return {"ok": False, "error": "prowlarr_not_configured"}
        try:
            prowlarr = await clients.prowlarr()
            indexers = await prowlarr.indexers()
        except ReeloomError as error:
            return {"ok": False, "error": error.code}
        return {"ok": True, "indexers": indexers}

    @api.post("/settings/test-pt-client")
    async def test_pt_client():
        if clients is None:
            return {"ok": False, "error": "pt_client_not_configured"}
        try:
            client = await clients.pt_client()
            version = await client.version()
        except ReeloomError as error:
            return {"ok": False, "error": error.code}
        return {"ok": True, "version": version}

    # ---- subscriptions ------------------------------------------------

    def need_subscriptions():
        if subscriptions is None:
            raise HTTPException(status_code=503, detail="subscriptions_not_available")
        return subscriptions

    async def check_subscription_target(config_id: str) -> None:
        config = await database.get_config(config_id)
        if config is None:
            raise HTTPException(status_code=422, detail="config_not_found")
        if config.media_type is MediaType.MOVIE:
            raise HTTPException(status_code=422, detail="subscription_needs_series_watch")

    def check_patterns(*patterns: str | None) -> None:
        for pattern in patterns:
            if pattern:
                try:
                    compile_pattern(pattern)
                except ReeloomError as error:
                    raise HTTPException(status_code=422, detail=error.code)

    async def subscription_json(subscription: SubscriptionModel) -> dict[str, Any]:
        grabs = await database.list_grabs(subscription_id=subscription.id, limit=200)
        return {
            **subscription.to_json(),
            "grabs": [grab.to_json() for grab in grabs],
        }

    @api.get("/subscriptions")
    async def list_subscriptions():
        return {
            "subscriptions": [
                await subscription_json(item)
                for item in await database.list_subscriptions()
            ]
        }

    @api.post("/subscriptions", status_code=201)
    async def create_subscription(payload: SubscriptionInput):
        await check_subscription_target(payload.config_id)
        check_patterns(payload.include, payload.exclude)
        name = (payload.name or "").strip()
        if not name and clients is not None:
            try:
                tmdb = await clients.tmdb()
                name = (await tmdb.get_series(payload.tmdb_id)).get("title") or ""
            except ReeloomError:
                name = ""
        subscription = SubscriptionModel(
            id=str(uuid.uuid4()),
            name=name or payload.query,
            config_id=payload.config_id,
            tmdb_id=payload.tmdb_id,
            query=payload.query.strip(),
            season=payload.season,
            groups=tuple(item.strip() for item in payload.groups if item.strip()),
            include=payload.include,
            exclude=payload.exclude,
            notes=payload.notes,
            enabled=payload.enabled,
            ignore_before=None if payload.backfill else datetime.now(timezone.utc),
        )
        created = await database.create_subscription(subscription)
        return await subscription_json(created)

    @api.patch("/subscriptions/{subscription_id}")
    async def update_subscription(subscription_id: str, payload: SubscriptionPatch):
        if await database.get_subscription(subscription_id) is None:
            raise HTTPException(status_code=404, detail="subscription_not_found")
        values = payload.model_dump(exclude_none=True)
        if values.pop("clear_season", False):
            values["season"] = None
        if "config_id" in values:
            await check_subscription_target(values["config_id"])
        check_patterns(values.get("include"), values.get("exclude"))
        if "groups" in values:
            values["groups"] = [item.strip() for item in values["groups"] if item.strip()]
        await database.update_subscription(subscription_id, values)
        stored = await database.get_subscription(subscription_id)
        assert stored is not None
        return await subscription_json(stored)

    @api.delete("/subscriptions/{subscription_id}")
    async def delete_subscription(subscription_id: str):
        live = await database.list_grabs(
            subscription_id=subscription_id,
            states=[GrabState.DOWNLOADING, GrabState.COMPLETED],
        )
        if live:
            raise HTTPException(status_code=409, detail="subscription_has_live_grabs")
        await database.delete_subscription(subscription_id)
        return {"deleted": True}

    @api.post("/subscriptions/{subscription_id}/check")
    async def check_subscription(subscription_id: str):
        service = need_subscriptions()
        try:
            grabs = await service.check_now(subscription_id)
        except ReeloomError as error:
            status = 404 if error.code == "subscription_not_found" else 502
            if error.code.endswith("_not_configured"):
                status = 503
            raise HTTPException(status_code=status, detail=error.code)
        return {"grabbed": [grab.to_json() for grab in grabs]}

    @api.post("/subscriptions/preview")
    async def preview_subscription(payload: SubscriptionInput):
        """What a check would grab for these settings, without grabbing."""

        service = need_subscriptions()
        check_patterns(payload.include, payload.exclude)
        draft = SubscriptionModel(
            id="",
            name=payload.name or payload.query,
            config_id=payload.config_id,
            tmdb_id=payload.tmdb_id,
            query=payload.query.strip(),
            season=payload.season,
            groups=tuple(item.strip() for item in payload.groups if item.strip()),
            include=payload.include,
            exclude=payload.exclude,
            ignore_before=None if payload.backfill else datetime.now(timezone.utc),
        )
        try:
            verdicts = await service.preview(draft)
        except ReeloomError as error:
            status = 503 if error.code.endswith("_not_configured") else 502
            raise HTTPException(status_code=status, detail=error.code)
        return {"releases": [item.to_json() for item in verdicts]}

    @api.post("/subscriptions/{subscription_id}/preview")
    async def preview_existing(subscription_id: str):
        service = need_subscriptions()
        subscription = await database.get_subscription(subscription_id)
        if subscription is None:
            raise HTTPException(status_code=404, detail="subscription_not_found")
        try:
            verdicts = await service.preview(subscription)
        except ReeloomError as error:
            status = 503 if error.code.endswith("_not_configured") else 502
            raise HTTPException(status_code=status, detail=error.code)
        return {"releases": [item.to_json() for item in verdicts]}

    @api.delete("/grabs/{grab_id}")
    async def forget_grab(grab_id: str):
        service = need_subscriptions()
        try:
            await service.forget(grab_id)
        except ReeloomError as error:
            status = 404 if error.code == "grab_not_found" else 502
            raise HTTPException(status_code=status, detail=error.code)
        return {"deleted": True}

    @api.get("/tmdb/search")
    async def tmdb_search(query: str, movie: bool = False):
        """TMDB lookup for the subscription form."""

        if clients is None:
            raise HTTPException(status_code=503, detail="tmdb_not_configured")
        if not query.strip() or len(query) > 200:
            raise HTTPException(status_code=422, detail="invalid_query")
        try:
            tmdb = await clients.tmdb()
            result = await tmdb.search(query.strip(), movie=movie)
        except ReeloomError as error:
            status = 503 if error.code.endswith("_not_configured") else 502
            raise HTTPException(status_code=status, detail=error.code)
        return result.to_json()

    # ---- magnet downloads ---------------------------------------------

    @api.post("/downloads", status_code=201)
    async def add_download(payload: DownloadInput):
        if downloads is None:
            raise HTTPException(status_code=503, detail="downloads_not_available")
        directory = payload.directory.strip()
        if len(directory) > 1:
            directory = directory.rstrip("/")
        try:
            download = await downloads.submit(payload.magnet.strip(), directory)
        except ReeloomError as error:
            raise _download_error(error)
        nudge()
        return download.to_json()

    # ---- daily subtitle recheck ----------------------------------------

    @api.get("/subtitle-rechecks")
    async def list_subtitle_rechecks():
        """Finished runs the daily subtitle search still cares about: the
        ones being searched right now, the ones waiting for their next
        daily look, and the ones whose window closed without a find."""

        settings = await database.get_settings()
        days = int(
            settings.get("subtitle_recheck_days", DEFAULT_SUBTITLE_RECHECK_DAYS)
            or 0
        )
        now = time.time()
        items = await subtitle_recheck_status(database, now=now, days=days)
        order = {RECHECK_SEARCHING: 0, RECHECK_WAITING: 1, RECHECK_GIVEN_UP: 2}
        items.sort(
            key=lambda item: (
                order.get(item.status, 3),
                item.next_at if item.next_at is not None else -item.deadline,
            )
        )
        return {
            "now": now,
            "days": days,
            "items": [item.to_json() for item in items],
        }

    @api.get("/downloads")
    async def list_downloads(limit: int = 100):
        items = await database.list_magnet_downloads(limit=min(limit, 500))
        return {
            "now": time.time(),
            "downloads": [item.to_json() for item in items],
            "dirs": await database.magnet_download_dirs(),
        }

    @api.post("/downloads/{download_id}/delete")
    async def delete_download(download_id: str):
        """Remove the task at CloudDrive2, downloaded data included, and
        close the row. POST, not DELETE: this has a cloud-side effect."""

        if downloads is None:
            raise HTTPException(status_code=503, detail="downloads_not_available")
        try:
            download = await downloads.remove(download_id)
        except ReeloomError as error:
            raise _download_error(error)
        return download.to_json()

    @api.post("/downloads/{download_id}/retry")
    async def retry_download(download_id: str):
        if downloads is None:
            raise HTTPException(status_code=503, detail="downloads_not_available")
        try:
            download = await downloads.retry(download_id)
        except ReeloomError as error:
            raise _download_error(error)
        nudge()
        return download.to_json()

    @api.delete("/downloads/{download_id}")
    async def delete_download_row(download_id: str):
        """Prune one concluded row from history. DB only; never touches
        CloudDrive."""

        download = await database.get_magnet_download(download_id)
        if download is None:
            raise HTTPException(status_code=404, detail="download_not_found")
        if not download.state.is_terminal:
            raise HTTPException(status_code=409, detail="download_not_terminal")
        await database.delete_magnet_download(download_id)
        return {"deleted": True}

    @api.get("/clouddrive/dirs")
    async def list_cloud_dirs(path: str = "/"):
        """List CloudDrive2 subdirectories, for the download dir picker.
        Same response shape as /api/fs/dirs so the picker is reusable."""

        if clients is None:
            raise HTTPException(
                status_code=503, detail="clouddrive_not_configured"
            )
        try:
            validate_api_path(path, allow_root=True)
            cloud = await clients.clouddrive()
            entries = await cloud.list_directory(path, force_refresh=False)
        except ReeloomError as error:
            raise _download_error(error)
        dirs = sorted(
            (str(entry["name"]) for entry in entries if entry["is_directory"]),
            key=str.casefold,
        )
        parent = None if path == "/" else posixpath.dirname(path)
        return {"path": path, "parent": parent, "dirs": dirs}

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    app.include_router(api)

    if downloads is not None and clients is not None:
        from reeloom.server.qbittorrent import create_qbittorrent_router

        app.include_router(
            create_qbittorrent_router(
                database=database,
                downloads=downloads,
                clients=clients,
                wake=nudge,
            )
        )

    if static_dir is not None and static_dir.is_dir():
        _mount_ui(app, static_dir)

    return app


def _mount_ui(app: FastAPI, static_dir: Path) -> None:
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    index = static_dir / "index.html"
    app.mount(
        "/assets",
        StaticFiles(directory=static_dir / "assets", check_dir=False),
        name="assets",
    )

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not_found")
        return FileResponse(index)


# CloudDrive-side failures not listed here surface as 502 with the stable
# error code as the detail.
_DOWNLOAD_ERROR_STATUS = {
    "invalid_magnet": 422,
    "clouddrive_invalid_path": 422,
    "duplicate_download": 409,
    "download_not_found": 404,
    "download_not_retryable": 409,
    "download_is_moving": 409,
    "download_not_live": 409,
    "clouddrive_not_configured": 503,
    "clouddrive_path_not_found": 404,
}


def _download_error(error: ReeloomError) -> HTTPException:
    status = _DOWNLOAD_ERROR_STATUS.get(error.code, 502)
    return HTTPException(status_code=status, detail=error.code)


def _check_roots(inbound: str | None, library: str | None) -> None:
    for value in (inbound, library):
        if value is not None and not Path(value).is_absolute():
            raise HTTPException(status_code=422, detail="roots_must_be_absolute")


def _check_extra_dirs(dirs: list[str], inbound: str, library: str) -> None:
    """Extra replacement-detection dirs must be absolute and disjoint from the
    watch roots — a nested dir would let one run trash files it also moves."""

    roots = [Path(inbound), Path(library)]
    seen: set[str] = set()
    for value in dirs:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise HTTPException(status_code=422, detail="extra_dirs_must_be_absolute")
        key = str(path)
        if key in seen:
            raise HTTPException(status_code=422, detail="extra_dir_duplicate")
        seen.add(key)
        for root in roots:
            if path == root or root in path.parents or path in root.parents:
                raise HTTPException(
                    status_code=422, detail="extra_dir_overlaps_root"
                )


def _config_json(config: WatchConfigModel | None) -> dict[str, Any]:
    if config is None:
        raise HTTPException(status_code=404, detail="config_not_found")
    return {
        "id": config.id,
        "name": config.name,
        "inbound_root": config.inbound_root,
        "library_root": config.library_root,
        "media_type": config.media_type.value,
        "enabled": config.enabled,
        "stability_seconds": config.stability_seconds,
        "acquire_subtitles": config.acquire_subtitles,
        "subtitle_variant": config.subtitle_variant.value,
        "notify": config.notify,
        "replace_enabled": config.replace_enabled,
        "replace_extra_dirs": list(config.replace_extra_dirs),
        "replace_auto_ratio": config.replace_auto_ratio,
        "cross_filesystem": config.cross_filesystem,
    }


def _run_summary(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "config_id": run.config_id,
        "folder_name": run.folder_name,
        "state": run.state.value,
        "title": run.plan.identity.title if run.plan else None,
        "year": run.plan.identity.year if run.plan else None,
        "tmdb_id": run.plan.identity.tmdb_id if run.plan else None,
        "file_count": len(run.snapshot),
        "move_count": len(run.plan.moves) if run.plan else 0,
        "result": run.result.to_json() if run.result else None,
        "error": run.error,
        "attempts": run.attempts,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }
