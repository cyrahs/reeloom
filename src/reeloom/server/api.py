"""HTTP API.

Single admin, same-origin UI, no sessions: one Bearer token guards every
route. The UI polls; there is no event stream to reconnect, buffer or replay.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from reeloom.db import Database
from reeloom.models import MediaType, Run, RunState, SubtitleVariant
from reeloom.models import WatchConfig as WatchConfigModel

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


class SettingsInput(BaseModel):
    tmdb_api_key: str | None = Field(default=None, max_length=200)
    llm_base_url: str | None = Field(default=None, max_length=500)
    llm_api_key: str | None = Field(default=None, max_length=500)
    llm_model: str | None = Field(default=None, max_length=200)
    # Empty string means "provider default": the parameter is not sent at all.
    llm_reasoning_effort: Literal["", "minimal", "low", "medium", "high"] | None = None
    telegram_bot_token: str | None = Field(default=None, max_length=200)
    telegram_chat_id: str | None = Field(default=None, max_length=64)
    trash_retention_days: int | None = Field(default=None, ge=0, le=365)


class MessageInput(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


class ReplaceResolveInput(BaseModel):
    action: Literal["replace", "discard_incoming", "keep_both"]


def create_app(
    *,
    database: Database,
    admin_token: str,
    worker=None,
    answerer=None,
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
            "trash_retention_days": stored.get("trash_retention_days", 3),
            "tmdb_api_key_set": bool(stored.get("tmdb_api_key")),
            "llm_api_key_set": bool(stored.get("llm_api_key")),
            "telegram_bot_token_set": bool(stored.get("telegram_bot_token")),
        }

    @api.put("/settings")
    async def put_settings(payload: SettingsInput):
        await database.update_settings(payload.model_dump(exclude_none=True))
        nudge()
        return {"updated": True}

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    app.include_router(api)

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
    }
