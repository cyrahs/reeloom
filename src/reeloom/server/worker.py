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
# A subtitle release for a freshly aired show tends to appear days or weeks
# after the video, so a finished run still short of subtitles is searched
# again once a day. The pass itself runs more often and arms at most one run
# each time, which spreads the forum and model traffic over the day.
SUBTITLE_RECHECK_PASS_SECONDS = 600
SUBTITLE_RECHECK_INTERVAL_SECONDS = 86400
DEFAULT_SUBTITLE_RECHECK_DAYS = 30


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

    async def subtitles_rechecked(self, run: Run, config: WatchConfig) -> None:
        """A daily recheck found subtitles for an already finished run."""

    async def subtitles_given_up(self, run: Run, config: WatchConfig) -> None:
        """The recheck window closed and the run still lacks subtitles."""


class DownloadPoller(Protocol):
    async def poll(self) -> None: ...


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
        downloads: DownloadPoller | None = None,
        scan_interval_seconds: int = 30,
    ) -> None:
        self._db = database
        self._identifier = identifier
        self._executor = executor
        self._comparer = comparer
        self._subtitles = subtitles
        self._notifier = notifier
        self._tracker = tracker or StabilityTracker()
        self._downloads = downloads
        self._scan_interval = scan_interval_seconds
        self._wake = asyncio.Event()
        self._intake: list[IntakeFolder] = []
        self._last_purge = float("-inf")
        self._last_download_poll = float("-inf")
        self._last_subtitle_recheck = float("-inf")

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
            await self._maybe_poll_downloads()
            progressed |= await self._maybe_recheck_subtitles()
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
        if not run.executed_moves:
            run = await self._refresh_snapshot(run, config)
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

    async def _refresh_snapshot(self, run: Run, config: WatchConfig) -> Run:
        """Re-read the intake folder before (re-)identifying.

        A run's snapshot is taken when the folder settles. By the time a
        human retries or revises a parked run they have usually changed the
        folder — pulled out files that belong elsewhere, dropped in a missing
        episode — and the model has to see the folder as it is now, not the
        listing that led to the previous verdict. Only unexecuted runs are
        rescanned: once files have moved into the library the intake folder
        no longer describes the run, and the ledger does.
        """

        path = Path(config.inbound_root) / run.folder_name
        try:
            snapshot = tuple(snapshot_folder(path))
        except ReeloomError as error:
            raise NeedsAttention(error.code, **error.context) from error
        if not snapshot:
            raise NeedsAttention("folder_missing", folder=run.folder_name)
        if not any(item.kind is FileKind.VIDEO for item in snapshot):
            raise NeedsAttention("no_video", folder=run.folder_name)
        if snapshot == run.snapshot:
            return run
        before = {item.relative_path for item in run.snapshot}
        after = {item.relative_path for item in snapshot}
        await self._db.set_snapshot(run.id, snapshot)
        # The UI shows only the message, so the counts live in it.
        await self._db.log(
            run.id,
            f"folder rescanned: {len(snapshot)} file(s),"
            f" {len(after - before)} added, {len(before - after)} removed",
            data={
                "added": sorted(after - before),
                "removed": sorted(before - after),
            },
        )
        return replace(run, snapshot=snapshot)

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
        return self._subtitles is not None and wants_subtitles(config)

    async def _acquire(self, run: Run, config: WatchConfig) -> None:
        assert self._subtitles is not None
        result = run.result or RunResult()
        recheck = dict(run.extra.get("subtitle_recheck") or {})
        rechecking = bool(recheck.pop("pending", False))
        acquired_before = result.subtitles_acquired
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
        if not rechecking:
            await self._settle(run, config)
            return
        # A recheck that found nothing settles quietly: the run already had
        # its notification, and a daily "still nothing" would only be noise.
        await self._db.set_extra(
            run.id, {**run.extra, "subtitle_recheck": recheck}
        )
        found = result.subtitles_acquired - acquired_before
        if found > 0:
            await self._db.log(
                run.id, f"subtitle recheck published {found} file(s)"
            )
        else:
            await self._db.log(
                run.id, f"subtitle recheck found nothing: {result.subtitle_note}"
            )
        await self._settle(run, config, notify="rechecked" if found > 0 else None)

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

    async def _settle(
        self,
        run: Run,
        config: WatchConfig,
        *,
        notify: str | None = "settled",
    ) -> None:
        await self._db.set_state(run.id, RunState.DONE)
        await self._db.log(run.id, "done")
        if notify is not None:
            await self._notify(run.id, config, kind=notify)
        await self._purge_after_settle(run, config)

    # ---- daily subtitle recheck -----------------------------------------

    async def _maybe_recheck_subtitles(self) -> bool:
        """Send one finished run still short of subtitles back through
        acquisition, at most once per pass, and warn about the ones whose
        window ran out. True if a run was armed."""

        if self._subtitles is None:
            return False
        now = time.monotonic()
        if now - self._last_subtitle_recheck < SUBTITLE_RECHECK_PASS_SECONDS:
            return False
        self._last_subtitle_recheck = now
        try:
            return await self._recheck_pass()
        except Exception:
            _LOGGER.exception("subtitle recheck failed")
            return False

    async def _recheck_pass(self) -> bool:
        settings = await self._db.get_settings()
        days = int(
            settings.get("subtitle_recheck_days", DEFAULT_SUBTITLE_RECHECK_DAYS)
            or 0
        )
        if days <= 0:
            return False
        now = time.time()
        items = await subtitle_recheck_status(self._db, now=now, days=days)
        for item in items:
            if item.status == RECHECK_GIVEN_UP and not item.warned:
                await self._give_up_on_subtitles(item)
        if await self._db.next_active_run() is not None:
            # Never queue behind, or ahead of, a fresh download.
            return False
        due = [
            item
            for item in items
            if item.status == RECHECK_WAITING
            and item.next_at is not None
            and item.next_at <= now
        ]
        if not due:
            return False
        item = min(due, key=lambda item: item.next_at or 0.0)
        run = item.run
        count = item.count + 1
        await self._db.set_extra(
            run.id,
            {
                **run.extra,
                "subtitle_recheck": {
                    "count": count,
                    "last_at": now,
                    "pending": True,
                },
            },
        )
        await self._db.log(
            run.id,
            f"subtitle recheck #{count}: searching again",
            data={"note": run.result.subtitle_note if run.result else ""},
        )
        await self._db.set_state(run.id, RunState.ACQUIRING_SUBS)
        _LOGGER.info("subtitle recheck #%d armed run=%s", count, run.id)
        return True

    async def _give_up_on_subtitles(self, item: SubtitleRecheck) -> None:
        """The window closed with subtitles still missing: say so once."""

        run = item.run
        record = dict(run.extra.get("subtitle_recheck") or {})
        record["given_up"] = True
        await self._db.set_extra(
            run.id, {**run.extra, "subtitle_recheck": record}
        )
        note = run.result.subtitle_note if run.result else ""
        await self._db.log(
            run.id,
            f"subtitle recheck gave up after {item.count} attempt(s): {note}",
            level="warning",
        )
        _LOGGER.warning(
            "subtitle recheck gave up run=%s attempts=%d", run.id, item.count
        )
        await self._notify(run.id, item.config, kind="given_up")

    # ---- magnet download tracking --------------------------------------

    async def _maybe_poll_downloads(self) -> None:
        """Track CloudDrive2 downloads, at most once per scan interval.

        The monotonic guard keeps an active-run busy loop from hammering
        CloudDrive; like the purge, this contributes nothing to
        ``progressed``.
        """

        if self._downloads is None:
            return
        now = time.monotonic()
        if now - self._last_download_poll < self._scan_interval:
            return
        self._last_download_poll = now
        try:
            await self._downloads.poll()
        except Exception:
            _LOGGER.exception("download poll failed")

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

    async def _notify(
        self, run_id: str, config: WatchConfig, *, kind: str = "settled"
    ) -> None:
        if self._notifier is None or not config.notify:
            return
        run = await self._db.get_run(run_id)
        if run is None:
            return
        try:
            if kind == "rechecked":
                await self._notifier.subtitles_rechecked(run, config)
            elif kind == "given_up":
                await self._notifier.subtitles_given_up(run, config)
            else:
                await self._notifier.run_settled(run, config)
        except Exception:
            _LOGGER.warning("notification failed run=%s", run_id, exc_info=True)


# ---- daily subtitle recheck: shared status ---------------------------------

RECHECK_WAITING = "waiting"
RECHECK_SEARCHING = "searching"
RECHECK_GIVEN_UP = "given_up"


@dataclass(frozen=True, slots=True)
class SubtitleRecheck:
    """One finished run that still wants subtitles, and where its daily
    search stands. ``waiting`` runs are searched again at ``next_at``,
    ``searching`` ones are in acquisition right now, and ``given_up`` ones
    ran out their window (``warned`` once the notification went out)."""

    run: Run
    config: WatchConfig
    status: str
    count: int
    last_at: float | None
    next_at: float | None
    deadline: float
    warned: bool = False

    def to_json(self) -> dict[str, Any]:
        plan = self.run.plan
        result = self.run.result
        assert plan is not None and result is not None
        return {
            "run_id": self.run.id,
            "config_name": self.config.name,
            "folder_name": self.run.folder_name,
            "title": plan.identity.title,
            "year": plan.identity.year,
            "tmdb_id": plan.identity.tmdb_id,
            "status": self.status,
            "count": self.count,
            "last_at": self.last_at,
            "next_at": self.next_at,
            "deadline": self.deadline,
            "note": result.subtitle_note,
            "created_at": (
                self.run.created_at.isoformat() if self.run.created_at else None
            ),
        }


def wants_subtitles(config: WatchConfig) -> bool:
    return config.acquire_subtitles and config.media_type is MediaType.ANIME


async def subtitle_recheck_status(
    database: Database, *, now: float, days: int
) -> list[SubtitleRecheck]:
    """Every run the daily subtitle search still cares about.

    A run qualifies when it finished with a non-empty subtitle note (some
    episode wanted a subtitle and did not get one) on an enabled anime watch
    with acquisition on. Runs whose window closed before they were ever
    rechecked are left out: there is nothing to report about them.
    """

    if days <= 0:
        return []
    window_start = now - days * 86400
    configs: dict[str, WatchConfig | None] = {}
    items: list[SubtitleRecheck] = []
    runs = await database.list_runs(
        states=[RunState.DONE, RunState.ACQUIRING_SUBS], limit=500
    )
    for run in runs:
        if run.plan is None or run.result is None:
            continue
        if not run.result.subtitle_note:
            # Empty note: nothing was wanted, or everything was found.
            continue
        record = run.extra.get("subtitle_recheck") or {}
        pending = bool(record.get("pending"))
        if run.state is RunState.ACQUIRING_SUBS and not pending:
            # The run's own first acquisition, not a recheck.
            continue
        count = int(record.get("count", 0))
        created = run.created_at.timestamp() if run.created_at else 0.0
        expired = created < window_start
        if expired and count == 0:
            continue
        if run.config_id not in configs:
            configs[run.config_id] = await database.get_config(run.config_id)
        config = configs[run.config_id]
        if config is None or not config.enabled or not wants_subtitles(config):
            continue
        last = record.get("last_at")
        last_at = float(last) if isinstance(last, (int, float)) else None
        if pending:
            status, next_at = RECHECK_SEARCHING, None
        elif expired:
            status, next_at = RECHECK_GIVEN_UP, None
        else:
            status = RECHECK_WAITING
            baseline = last_at if last_at is not None else _settled_at(run)
            next_at = baseline + SUBTITLE_RECHECK_INTERVAL_SECONDS
        items.append(
            SubtitleRecheck(
                run=run,
                config=config,
                status=status,
                count=count,
                last_at=last_at,
                next_at=next_at,
                deadline=created + days * 86400,
                warned=bool(record.get("given_up")),
            )
        )
    return items


def _settled_at(run: Run) -> float:
    stamp = run.updated_at or run.created_at
    return stamp.timestamp() if stamp is not None else 0.0
