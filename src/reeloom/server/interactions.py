from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from agents.items import TResponseInputItem

from reeloom.runtime.budget import RunBudget
from reeloom.server.session import _copy
from reeloom.server.errors import ServerError, ServerErrorCode

_MAX_MESSAGE_BYTES = 16 * 1024
_MAX_REPLY_BYTES = 64 * 1024


class InteractionKind(StrEnum):
    QUESTION = "question"
    REVISION = "revision"
    REAPPLY = "reapply"


@dataclass(frozen=True, slots=True)
class InteractionResult:
    interaction_id: str
    kind: InteractionKind
    assistant_reply: str
    plan_hash: str | None
    model_tokens: int


@dataclass(frozen=True, slots=True)
class InteractionReservation:
    interaction_id: str
    run_id: str
    kind: InteractionKind
    request_hash: str
    session_revision: int
    plan_hash: str
    budget: RunBudget = RunBudget()
    terminal_result: InteractionResult | None = None


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    reservation: InteractionReservation
    message: str


@dataclass(frozen=True, slots=True)
class InteractionExecution:
    assistant_reply: str
    session_revision: int
    model_tokens: int
    model_turns: int = 1
    tool_calls: int = 0
    failures: int = 0
    domain_events: tuple[str, ...] = ()
    plan_hash: str | None = None
    fresh_mapping_submitted: bool = False
    session_batch: tuple[TResponseInputItem, ...] = ()
    session_items: tuple[TResponseInputItem, ...] = ()
    lineage_parent_hash: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.assistant_reply, str)
            or len(self.assistant_reply.encode("utf-8")) > _MAX_REPLY_BYTES
            or type(self.session_revision) is not int
            or self.session_revision < 1
            or type(self.model_tokens) is not int
            or self.model_tokens < 0
            or type(self.model_turns) is not int
            or self.model_turns < 1
            or type(self.tool_calls) is not int
            or self.tool_calls < 0
            or type(self.failures) is not int
            or self.failures < 0
            or not isinstance(self.domain_events, tuple)
            or any(
                not isinstance(item, str) or not item
                for item in self.domain_events
            )
            or (
                self.lineage_parent_hash is not None
                and (
                    not isinstance(self.lineage_parent_hash, str)
                    or len(self.lineage_parent_hash.encode("utf-8")) > 128
                )
            )
        ):
            raise ServerError(
                ServerErrorCode.INTERACTION_INVALID_RESULT
            )
        try:
            _copy(list(self.session_batch))
            _copy(list(self.session_items))
        except Exception:
            raise ServerError(
                ServerErrorCode.INTERACTION_INVALID_RESULT
            ) from None


class InteractionRepository(Protocol):
    def reserve(
        self,
        *,
        run_id: str,
        kind: InteractionKind,
        idempotency_key: str,
        expected_plan_hash: str,
        message: str,
    ) -> InteractionReservation: ...

    def finalize(
        self,
        *,
        reservation: InteractionReservation,
        execution: InteractionExecution,
    ) -> InteractionResult: ...

    def fail(self, *, interaction_id: str) -> None: ...


def _request_hash(
    *,
    kind: InteractionKind,
    expected_plan_hash: str,
    message: str,
) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            {
                "expected_plan_hash": expected_plan_hash,
                "kind": kind.value,
                "message": message,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


class InMemoryInteractionRepository:
    def __init__(
        self,
        *,
        run_id: str,
        plan_hash: str,
        session_revision: int,
    ) -> None:
        self._lock = threading.RLock()
        self._run_id = run_id
        self.plan_hash = plan_hash
        self.session_revision = session_revision
        self.plan_versions = 1
        self.domain_event_count = 0
        self._active: str | None = None
        self._records: dict[
            str,
            tuple[str, InteractionReservation, InteractionResult | None],
        ] = {}

    def reserve(
        self,
        *,
        run_id: str,
        kind: InteractionKind,
        idempotency_key: str,
        expected_plan_hash: str,
        message: str,
    ) -> InteractionReservation:
        if (
            run_id != self._run_id
            or not isinstance(kind, InteractionKind)
            or not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key.encode("utf-8")) > 256
            or not isinstance(message, str)
            or not message
            or len(message.encode("utf-8")) > _MAX_MESSAGE_BYTES
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        digest = _request_hash(
            kind=kind,
            expected_plan_hash=expected_plan_hash,
            message=message,
        )
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                old_hash, reservation, result = existing
                if old_hash != digest:
                    raise ServerError(
                        ServerErrorCode.INTERACTION_CONFLICT
                    )
                return InteractionReservation(
                    interaction_id=reservation.interaction_id,
                    run_id=reservation.run_id,
                    kind=reservation.kind,
                    request_hash=reservation.request_hash,
                    session_revision=reservation.session_revision,
                    plan_hash=reservation.plan_hash,
                    terminal_result=result,
                )
            if self._active is not None:
                raise ServerError(ServerErrorCode.RUN_BUSY)
            if expected_plan_hash != self.plan_hash:
                raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
            reservation = InteractionReservation(
                interaction_id=f"interaction-{uuid.uuid4().hex}",
                run_id=run_id,
                kind=kind,
                request_hash=digest,
                session_revision=self.session_revision,
                plan_hash=self.plan_hash,
            )
            self._records[idempotency_key] = (
                digest,
                reservation,
                None,
            )
            self._active = reservation.interaction_id
            return reservation

    def finalize(
        self,
        *,
        reservation: InteractionReservation,
        execution: InteractionExecution,
    ) -> InteractionResult:
        with self._lock:
            if self._active != reservation.interaction_id:
                raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
            if execution.session_revision != self.session_revision + 1:
                raise ServerError(ServerErrorCode.INTERACTION_INVALID_RESULT)
            if reservation.kind is InteractionKind.QUESTION:
                if (
                    execution.domain_events
                    or execution.plan_hash is not None
                    or execution.fresh_mapping_submitted
                ):
                    raise ServerError(
                        ServerErrorCode.INTERACTION_INVALID_RESULT
                    )
            elif reservation.kind is InteractionKind.REVISION:
                if (
                    not execution.fresh_mapping_submitted
                    or "mapping_submitted"
                    not in execution.domain_events
                    or "plan_built" not in execution.domain_events
                    or execution.plan_hash is None
                    or execution.plan_hash == self.plan_hash
                    or execution.lineage_parent_hash != self.plan_hash
                ):
                    raise ServerError(
                        ServerErrorCode.FRESH_MAPPING_REQUIRED
                    )
            elif reservation.kind is InteractionKind.REAPPLY:
                if (
                    not execution.fresh_mapping_submitted
                    or "mapping_submitted"
                    not in execution.domain_events
                    or (
                        execution.plan_hash is None
                        and "plan_built" in execution.domain_events
                    )
                    or (
                        execution.plan_hash is not None
                        and (
                            "plan_built"
                            not in execution.domain_events
                            or execution.plan_hash == self.plan_hash
                            or execution.lineage_parent_hash is None
                        )
                    )
                ):
                    raise ServerError(
                        ServerErrorCode.FRESH_MAPPING_REQUIRED
                    )
            result = InteractionResult(
                interaction_id=reservation.interaction_id,
                kind=reservation.kind,
                assistant_reply=execution.assistant_reply,
                plan_hash=execution.plan_hash,
                model_tokens=execution.model_tokens,
            )
            for key, record in tuple(self._records.items()):
                if record[1].interaction_id == reservation.interaction_id:
                    self._records[key] = (
                        record[0],
                        record[1],
                        result,
                    )
                    break
            self.session_revision = execution.session_revision
            self.domain_event_count += len(execution.domain_events)
            if execution.plan_hash is not None:
                self.plan_hash = execution.plan_hash
                self.plan_versions += 1
            self._active = None
            return result

    def fail(self, *, interaction_id: str) -> None:
        with self._lock:
            if self._active == interaction_id:
                self._active = None

    def reconcile_active(self) -> int:
        with self._lock:
            if self._active is None:
                return 0
            self._active = None
            return 1


class InteractionService:
    def __init__(
        self,
        *,
        repository: InteractionRepository,
        execute: Callable[[InteractionRequest], InteractionExecution],
    ) -> None:
        self._repository = repository
        self._execute = execute

    def run(
        self,
        *,
        run_id: str,
        kind: InteractionKind,
        idempotency_key: str,
        expected_plan_hash: str,
        message: str,
    ) -> InteractionResult:
        reservation = self._repository.reserve(
            run_id=run_id,
            kind=kind,
            idempotency_key=idempotency_key,
            expected_plan_hash=expected_plan_hash,
            message=message,
        )
        if reservation.terminal_result is not None:
            return reservation.terminal_result
        try:
            execution = self._execute(
                InteractionRequest(
                    reservation=reservation,
                    message=message,
                )
            )
            return self._repository.finalize(
                reservation=reservation,
                execution=execution,
            )
        except Exception:
            self._repository.fail(
                interaction_id=reservation.interaction_id
            )
            raise
