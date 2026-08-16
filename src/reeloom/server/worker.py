"""The single background worker.

One loop does two things: turn settled folders into runs, and push one active
run forward by one step. There is no queue, no lease and no scheduler table —
with a single worker the run's ``state`` column is the whole coordination
mechanism.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from reeloom.db import Database
from reeloom.models import (
    Deferred,
    FileKind,
    MediaType,
    MoveKind,
    Plan,
    ReeloomError,
    Run,
    RunResult,
    RunState,
    WatchConfig,
)
from reeloom.scanner import (
    StabilityTracker,
    discover_folders,
    folder_shape,
    snapshot_folder,
)
from reeloom.trash import (
    TrashError,
    list_trash_entries,
    prune_trash,
    purge_run_trash,
)

_LOGGER = logging.getLogger(__name__)

PURGE_INTERVAL_SECONDS = 3600
DEFAULT_TRASH_RETENTION_DAYS = 3


class NeedsAttention(ReeloomError):
    """The Agent could not settle the folder; a human has to look."""


@dataclass(frozen=True, slots=True)
class IntakeFolder:
    """A discovered inbound folder that has not become a run yet.

    ``settling`` folders are waiting out the stability window, ``empty`` ones
    hold no files yet, and ``skipped`` ones were stable but rejected for the
    given reason. Rebuilt on every scan so the UI can show what the scanner
    is watching and when a folder will be picked up.
    """

    config_id: str
    config_name: str
    folder_name: str
    file_count: int
    total_bytes: int
    status: str
    reason: str | None = None
    remaining_seconds: float | None = None
    scanned_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "config_id": self.config_id,
            "config_name": self.config_name,
            "folder_name": self.folder_name,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "status": self.status,
            "reason": self.reason,
            "remaining_seconds": self.remaining_seconds,
            "scanned_at": self.scanned_at,
        }


class Identifier(Protocol):
    async def identify(self, run: Run, config: WatchConfig) -> Plan:
        """Return a compiled plan or raise NeedsAttention."""


class Comparer(Protocol):
    async def compare(self, run: Run, config: WatchConfig) -> Plan | None:
        """Return an augmented plan, None to keep the current one, or raise
        NeedsAttention to park the run for a replacement confirmation."""


class Executor(Protocol):
    async def execute(self, run: Run, config: WatchConfig) -> RunResult: ...

    async def revert(self, run: Run, config: WatchConfig) -> None: ...

    async def discard(self, run: Run, config: WatchConfig) -> int: ...


class SubtitleService(Protocol):
    async def acquire(
        self, run: Run, config: WatchConfig, result: RunResult
    ) -> RunResult: ...


class Notifier(Protocol):
    async def run_settled(self, run: Run, config: WatchConfig) -> None: ...


class Worker:
    def __init__(
        self,
        database: Database,
        *,
        identifier: Identifier,
        executor: Executor,
        comparer: Comparer | None = None,
        subtitles: SubtitleService | None = None,
        notifier: Notifier | None = None,
        tracker: StabilityTracker | None = None,
        scan_interval_seconds: int = 30,
    ) -> None:
        self._db = database
        self._identifier = identifier
        self._executor = executor
        self._comparer = comparer
        self._subtitles = subtitles
        self._notifier = notifier
        self._tracker = tracker or StabilityTracker()
        self._scan_interval = scan_interval_seconds
        self._wake = asyncio.Event()
        self._intake: list[IntakeFolder] = []
        self._last_purge = float("-inf")

    def wake(self) -> None:
        """Ask the loop to run a step now instead of waiting for the timer."""

        self._wake.set()

    def intake_status(self) -> list[IntakeFolder]:
        """What the last scan saw in the inbound roots, short of a run."""

        return self._intake

    async def run_forever(self) -> None:
        await self.recover()
        while True:
            try:
                progressed = await self.tick()
            except Exception:
                _LOGGER.exception("worker tick failed")
                progressed = False
            await self._maybe_purge()
            if progressed:
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), self._scan_interval)
            except TimeoutError:
                pass
            self._wake.clear()

    async def recover(self) -> None:
        """Re-arm runs interrupted mid-identification.

        Identification has no filesystem side effects, so the safe recovery is
        simply to run it again. Runs interrupted while executing or reverting
        are left alone: replaying their ledger is idempotent. COMPARING also
        needs nothing here — it has no filesystem side effects and is simply
        re-entered (the comparer recognizes an already-augmented plan).
        """

        for run in await self._db.list_runs(states=[RunState.IDENTIFYING]):
            _LOGGER.info("re-arming interrupted identification run=%s", run.id)
            await self._db.log(run.id, "restarted after interruption")
            await self._db.set_state(run.id, RunState.PENDING)

    async def tick(self) -> bool:
        """Do at most one unit of work. True if anything happened."""

        created = await self.scan()
        advanced = await self.advance()
        return created or advanced

    # ---- intake -------------------------------------------------------

    async def scan(self) -> bool:
        created = False
        intake: list[IntakeFolder] = []
        for config in await self._db.list_configs(enabled_only=True):
            try:
                created |= await self._scan_config(config, intake)
            except Exception:
                _LOGGER.exception("scan failed config=%s", config.id)
        self._intake = intake
        return created

    async def _scan_config(
        self, config: WatchConfig, intake: list[IntakeFolder]
    ) -> bool:
        root = Path(config.inbound_root)
        folders = discover_folders(root)
        if not folders:
            return False
        open_folders = await self._db.open_folder_names(config.id)

        created = False
        for name in folders:
            if name in open_folders:
                continue
            key = (config.id, name)
            shape = folder_shape(root / name)
            held = self._tracker.observe(key, shape)

            def note(
                status: str,
                reason: str | None = None,
                remaining: float | None = None,
            ) -> None:
                intake.append(
                    IntakeFolder(
                        config_id=config.id,
                        config_name=config.name,
                        folder_name=name,
                        file_count=shape.file_count,
                        total_bytes=shape.total_bytes,
                        status=status,
                        reason=reason,
                        remaining_seconds=remaining,
                        scanned_at=time.time(),
                    )
                )

            if shape.file_count == 0:
                note("empty")
                continue
            if held < config.stability_seconds:
                note("settling", remaining=config.stability_seconds - held)
                continue
            try:
                snapshot = snapshot_folder(root / name)
            except ReeloomError as error:
                _LOGGER.warning(
                    "skipping folder=%s code=%s", name, error.code
                )
                note("skipped", reason=error.code)
                continue
            if not any(item.kind is FileKind.VIDEO for item in snapshot):
                _LOGGER.debug("folder has no video, skipping: %s", name)
                note("skipped", reason="no_video")
                continue
            if tuple(snapshot) == await self._db.last_snapshot(config.id, name):
                # A settled run already saw exactly this content. Re-opening it
                # would loop forever on folders a failed run left behind.
                note("skipped", reason="unchanged")
                continue
            run = await self._db.create_run(
                config_id=config.id, folder_name=name, snapshot=snapshot
            )
            if run is None:
                continue
            self._tracker.forget(key)
            await self._db.log(
                run.id, "run created", data={"files": len(snapshot)}
            )
            _LOGGER.info("created run=%s folder=%s", run.id, name)
            created = True
        return created

    # ---- run advancement ----------------------------------------------

    async def advance(self) -> bool:
        run = await self._db.next_active_run()
        if run is None:
            return False
        config = await self._db.get_config(run.config_id)
        if config is None:
            await self._db.set_state(
                run.id, RunState.FAILED, error={"code": "config_deleted"}
            )
            return True
        try:
            await self._step(run, config)
        except Deferred as error:
            # Nothing is wrong with the run; the deployment is not ready.
            # Park it and wait to be woken — typically by the settings page.
            _LOGGER.info("run=%s deferred: %s", run.id, error.code)
            await self._db.set_state(run.id, RunState.PENDING)
            return False
        except NeedsAttention as error:
            _LOGGER.info("run=%s needs attention: %s", run.id, error.code)
            await self._db.log(
                run.id,
                f"needs attention: {error.code}",
                level="warning",
                data=error.context,
            )
            await self._db.set_state(
                run.id,
                RunState.NEEDS_ATTENTION,
                error={"code": error.code, **error.context},
                bump_attempts=True,
            )
            await self._notify(run.id, config)
        except Exception as error:
            _LOGGER.exception("run=%s failed", run.id)
            await self._db.log(run.id, f"failed: {error}", level="error")
            await self._db.set_state(
                run.id,
                RunState.FAILED,
                error={"code": getattr(error, "code", "unexpected"), "detail": str(error)},
                bump_attempts=True,
            )
            await self._notify(run.id, config)
        return True

    async def _step(self, run: Run, config: WatchConfig) -> None:
        match run.state:
            case RunState.PENDING:
                await self._db.set_state(run.id, RunState.IDENTIFYING)
            case RunState.IDENTIFYING:
                await self._identify(run, config)
            case RunState.COMPARING:
                await self._compare(run, config)
            case RunState.EXECUTING:
                await self._execute(run, config)
            case RunState.ACQUIRING_SUBS:
                await self._acquire(run, config)
            case RunState.REVERTING:
                await self._revert(run, config)
            case RunState.DISCARDING:
                await self._discard(run, config)
            case _:
                raise ReeloomError("unexpected_state", state=run.state.value)

    async def _identify(self, run: Run, config: WatchConfig) -> None:
        plan = await self._identifier.identify(run, config)
        await self._db.set_plan(run.id, plan)
        await self._db.log(
            run.id,
            f"planned {plan.identity.title} ({plan.identity.year})",
            data={"moves": len(plan.moves), "unmapped": len(plan.unmapped)},
        )
        # A revision of an already-executed run has to undo the old layout
        # first. Identifying before reverting means a failed revision leaves
        # the library exactly as it was.
        await self._db.set_state(
            run.id,
            RunState.REVERTING
            if run.executed_moves
            else self._post_plan_state(config),
        )

    def _wants_compare(self, config: WatchConfig) -> bool:
        return config.replace_enabled and self._comparer is not None

    def _post_plan_state(self, config: WatchConfig) -> RunState:
        # Every path into EXECUTING for a replace-enabled run goes through
        # COMPARING, so a replacement decision is always recomputed against
        # the filesystem as it is right now.
        return (
            RunState.COMPARING
            if self._wants_compare(config)
            else RunState.EXECUTING
        )

    async def _compare(self, run: Run, config: WatchConfig) -> None:
        if not self._wants_compare(config) or run.plan is None:
            # Toggled off (or lost its plan) while parked; fall through.
            await self._db.set_state(run.id, RunState.EXECUTING)
            return
        assert self._comparer is not None
        plan = await self._comparer.compare(run, config)
        if plan is not None and plan != run.plan:
            trash_moves = sum(
                1
                for move in plan.moves
                if move.kind
                in (MoveKind.TRASH_REPLACED, MoveKind.TRASH_DUPLICATE)
            )
            await self._db.log(
                run.id,
                "plan augmented for replacement",
                data={"trash_moves": trash_moves},
            )
            await self._db.set_plan(run.id, plan)
        await self._db.set_state(run.id, RunState.EXECUTING)

    async def _execute(self, run: Run, config: WatchConfig) -> None:
        if run.plan is None:
            raise ReeloomError("missing_plan")
        result = await self._executor.execute(run, config)
        await self._db.log(
            run.id,
            f"moved {result.moved} file(s)",
            data=result.to_json(),
        )
        if run.result is not None and run.executed_moves:
            # A retry of an executed run replays the ledger, and the pass
            # counts only what it newly did — folding it into the recorded
            # summary keeps the earlier counts instead of wiping them. Revert
            # clears the ledger, so a revised run starts a fresh summary.
            result = run.result.merge_replay(result)
        await self._db.set_result(run.id, result)
        if self._should_acquire(config):
            await self._db.set_state(run.id, RunState.ACQUIRING_SUBS)
        else:
            await self._settle(run, config)

    def _should_acquire(self, config: WatchConfig) -> bool:
        return (
            config.acquire_subtitles
            and self._subtitles is not None
            and config.media_type is MediaType.ANIME
        )

    async def _acquire(self, run: Run, config: WatchConfig) -> None:
        assert self._subtitles is not None
        result = run.result or RunResult()
        try:
            result = await self._subtitles.acquire(run, config, result)
        except Exception as error:
            # Subtitle acquisition is best-effort: a failure is reported, it
            # never holds up an otherwise finished run.
            _LOGGER.warning("subtitle acquisition failed run=%s: %s", run.id, error)
            await self._db.log(
                run.id, f"subtitle acquisition failed: {error}", level="warning"
            )
            # The note lands in the Telegram notification; keep it short and
            # leave the full error to the log lines above.
            result = replace(result, subtitle_note=f"失败：{str(error)[:120]}")
        await self._db.set_result(run.id, result)
        await self._settle(run, config)

    async def _revert(self, run: Run, config: WatchConfig) -> None:
        await self._executor.revert(run, config)
        await self._db.clear_executed(run.id)
        await self._db.log(run.id, "reverted previous layout")
        await self._db.set_state(run.id, self._post_plan_state(config))

    async def _discard(self, run: Run, config: WatchConfig) -> None:
        # An executed run (typically a done one being abandoned) is reverted
        # first, so the intake folder holds the original download again and
        # all of it — not just leftovers — ends up in the fail bucket.
        if run.executed_moves:
            await self._executor.revert(run, config)
            await self._db.clear_executed(run.id)
            await self._db.log(run.id, "reverted layout before discarding")
        moved = await self._executor.discard(run, config)
        await self._db.log(run.id, f"discarded {moved} file(s) to the fail bucket")
        await self._db.set_state(run.id, RunState.DISCARDED)
        await self._notify(run.id, config)
        await self._purge_after_settle(run, config)

    async def _settle(self, run: Run, config: WatchConfig) -> None:
        await self._db.set_state(run.id, RunState.DONE)
        await self._db.log(run.id, "done")
        await self._notify(run.id, config)
        await self._purge_after_settle(run, config)

    # ---- trash purging -------------------------------------------------

    async def _maybe_purge(self) -> None:
        """The only place trash is actually deleted, at most hourly."""

        now = time.monotonic()
        if now - self._last_purge < PURGE_INTERVAL_SECONDS:
            return
        self._last_purge = now
        try:
            await self._purge_pass()
        except Exception:
            _LOGGER.exception("trash purge failed")

    async def _purge_pass(self) -> None:
        settings = await self._db.get_settings()
        retention = settings.get(
            "trash_retention_days", DEFAULT_TRASH_RETENTION_DAYS
        )
        cutoff = time.time() - retention * 86400
        for root in await self._trash_roots():
            for entry in await asyncio.to_thread(list_trash_entries, root):
                await self._purge_entry(root, entry.run_id, entry.mtime, cutoff)
            await asyncio.to_thread(prune_trash, root)

    async def _purge_entry(
        self, root: Path, run_id: str, mtime: float, cutoff: float
    ) -> None:
        try:
            uuid.UUID(run_id)
        except ValueError:
            # Not something reeloom wrote; never delete what we don't own.
            _LOGGER.warning("unrecognized trash entry left alone: %s/%s", root, run_id)
            return
        run = await self._db.get_run(run_id)
        if run is not None and not run.state.is_terminal:
            return
        if mtime > cutoff:
            return
        await self._purge_run_trash(root, run_id)

    async def _purge_run_trash(self, root: Path, run_id: str) -> None:
        try:
            files, size = await asyncio.to_thread(purge_run_trash, root, run_id)
        except TrashError as error:
            _LOGGER.warning("trash purge refused: %s", error)
            return
        if files:
            _LOGGER.info(
                "purged trash run=%s root=%s files=%d bytes=%d",
                run_id,
                root,
                files,
                size,
            )
            if await self._db.get_run(run_id) is not None:
                await self._db.log(
                    run_id,
                    f"purged {files} file(s) from the trash area",
                    data={"bytes": size, "root": str(root)},
                )

    async def _purge_after_settle(self, run: Run, config: WatchConfig) -> None:
        """Retention 0 means storage is reclaimed the moment a run settles."""

        settings = await self._db.get_settings()
        retention = settings.get(
            "trash_retention_days", DEFAULT_TRASH_RETENTION_DAYS
        )
        if retention != 0:
            return
        await self._purge_run_trash(Path(config.inbound_root), run.id)

    async def _trash_roots(self) -> list[Path]:
        # Trash lives only under the watch roots; the library stays clean
        # because media servers scan it.
        roots: dict[str, Path] = {}
        for config in await self._db.list_configs():
            roots.setdefault(config.inbound_root, Path(config.inbound_root))
        return list(roots.values())

    async def _notify(self, run_id: str, config: WatchConfig) -> None:
        if self._notifier is None or not config.notify:
            return
        run = await self._db.get_run(run_id)
        if run is None:
            return
        try:
            await self._notifier.run_settled(run, config)
        except Exception:
            _LOGGER.warning("notification failed run=%s", run_id, exc_info=True)
