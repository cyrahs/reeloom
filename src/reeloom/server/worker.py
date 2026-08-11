"""The single background worker.

One loop does two things: turn settled folders into runs, and push one active
run forward by one step. There is no queue, no lease and no scheduler table —
with a single worker the run's ``state`` column is the whole coordination
mechanism.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from reeloom.db import Database
from reeloom.models import (
    Deferred,
    FileKind,
    MediaType,
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

_LOGGER = logging.getLogger(__name__)


class NeedsAttention(ReeloomError):
    """The Agent could not settle the folder; a human has to look."""


class Identifier(Protocol):
    async def identify(self, run: Run, config: WatchConfig) -> Plan:
        """Return a compiled plan or raise NeedsAttention."""


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
        subtitles: SubtitleService | None = None,
        notifier: Notifier | None = None,
        tracker: StabilityTracker | None = None,
        scan_interval_seconds: int = 30,
    ) -> None:
        self._db = database
        self._identifier = identifier
        self._executor = executor
        self._subtitles = subtitles
        self._notifier = notifier
        self._tracker = tracker or StabilityTracker()
        self._scan_interval = scan_interval_seconds
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """Ask the loop to run a step now instead of waiting for the timer."""

        self._wake.set()

    async def run_forever(self) -> None:
        await self.recover()
        while True:
            try:
                progressed = await self.tick()
            except Exception:
                _LOGGER.exception("worker tick failed")
                progressed = False
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
        are left alone: replaying their ledger is idempotent.
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
        for config in await self._db.list_configs(enabled_only=True):
            try:
                created |= await self._scan_config(config)
            except Exception:
                _LOGGER.exception("scan failed config=%s", config.id)
        return created

    async def _scan_config(self, config: WatchConfig) -> bool:
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
            if not self._tracker.is_stable(key, shape, config.stability_seconds):
                continue
            try:
                snapshot = snapshot_folder(root / name)
            except ReeloomError as error:
                _LOGGER.warning(
                    "skipping folder=%s code=%s", name, error.code
                )
                continue
            if not any(item.kind is FileKind.VIDEO for item in snapshot):
                _LOGGER.debug("folder has no video, skipping: %s", name)
                continue
            if tuple(snapshot) == await self._db.last_snapshot(config.id, name):
                # A settled run already saw exactly this content. Re-opening it
                # would loop forever on folders a failed run left behind.
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
            RunState.REVERTING if run.executed_moves else RunState.EXECUTING,
        )

    async def _execute(self, run: Run, config: WatchConfig) -> None:
        if run.plan is None:
            raise ReeloomError("missing_plan")
        result = await self._executor.execute(run, config)
        await self._db.set_result(run.id, result)
        await self._db.log(
            run.id,
            f"moved {result.moved} file(s)",
            data=result.to_json(),
        )
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
            result = replace(result, subtitle_note=f"failed: {error}")
        await self._db.set_result(run.id, result)
        await self._settle(run, config)

    async def _revert(self, run: Run, config: WatchConfig) -> None:
        await self._executor.revert(run, config)
        await self._db.clear_executed(run.id)
        await self._db.log(run.id, "reverted previous layout")
        await self._db.set_state(run.id, RunState.EXECUTING)

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

    async def _settle(self, run: Run, config: WatchConfig) -> None:
        await self._db.set_state(run.id, RunState.DONE)
        await self._db.log(run.id, "done")
        await self._notify(run.id, config)

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
