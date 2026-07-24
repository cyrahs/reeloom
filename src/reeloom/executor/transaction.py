from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.manifest import ExecutionManifest

CURRENT_TRANSACTION_SCHEMA_VERSION = "1"

_TRANSACTION_ID_PATTERN = re.compile(r"^txn-v1-[0-9a-f]{64}$")


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _transaction_id(
    *,
    plan_hash: str,
    approval_id: str,
    run_id: str,
) -> str:
    binding = _canonical_json(
        {
            "approval_id": approval_id,
            "plan_hash": plan_hash,
            "run_id": run_id,
            "schema_version": CURRENT_TRANSACTION_SCHEMA_VERSION,
        }
    )
    return f"txn-v1-{hashlib.sha256(binding).hexdigest()}"


@dataclass(frozen=True, slots=True)
class RollbackMove:
    candidate_id: str
    destination: str
    source: str
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int

    def payload(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "destination": self.destination,
            "source": self.source,
            "source_identity": {
                "ctime_ns": self.ctime_ns,
                "device": self.device,
                "inode": self.inode,
                "mtime_ns": self.mtime_ns,
                "size_bytes": self.size_bytes,
            },
        }


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    transaction_id: str
    plan_hash: str
    approval_id: str
    run_id: str
    rollback_moves: tuple[RollbackMove, ...]

    @classmethod
    def create(
        cls,
        manifest: ExecutionManifest,
        *,
        approval_id: str,
    ) -> TransactionRecord:
        sources = {
            source.candidate_id: source
            for source in manifest.sources
        }
        rollback_moves = tuple(
            RollbackMove(
                candidate_id=str(move.source_id),
                destination=move.destination.as_posix(),
                source=sources[move.source_id].relative_path.as_posix(),
                size_bytes=sources[move.source_id].size_bytes,
                device=sources[move.source_id].device,
                inode=sources[move.source_id].inode,
                mtime_ns=sources[move.source_id].mtime_ns,
                ctime_ns=sources[move.source_id].ctime_ns,
            )
            for move in reversed(manifest.moves)
        )
        return cls(
            transaction_id=_transaction_id(
                plan_hash=manifest.plan_hash,
                approval_id=approval_id,
                run_id=manifest.run_id,
            ),
            plan_hash=manifest.plan_hash,
            approval_id=approval_id,
            run_id=manifest.run_id,
            rollback_moves=rollback_moves,
        )

    @staticmethod
    def is_valid_id(value: object) -> bool:
        return (
            isinstance(value, str)
            and _TRANSACTION_ID_PATTERN.fullmatch(value) is not None
        )

    def verify_id(self) -> bool:
        return self.is_valid_id(
            self.transaction_id
        ) and hmac.compare_digest(
            self.transaction_id,
            _transaction_id(
                plan_hash=self.plan_hash,
                approval_id=self.approval_id,
                run_id=self.run_id,
            ),
        )

    def canonical_bytes(self) -> bytes:
        if not self.verify_id():
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return _canonical_json(
            {
                "approval_id": self.approval_id,
                "plan_hash": self.plan_hash,
                "rollback_moves": [
                    move.payload() for move in self.rollback_moves
                ],
                "run_id": self.run_id,
                "schema_version": CURRENT_TRANSACTION_SCHEMA_VERSION,
                "transaction_id": self.transaction_id,
            }
        )


def journal_event_bytes(
    transaction: TransactionRecord,
    *,
    event_type: str,
    candidate_id: str | None = None,
    failure_code: str | None = None,
) -> bytes:
    if not transaction.verify_id():
        raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
    return _canonical_json(
        {
            "candidate_id": candidate_id,
            "event_type": event_type,
            "failure_code": failure_code,
            "schema_version": CURRENT_TRANSACTION_SCHEMA_VERSION,
            "transaction_id": transaction.transaction_id,
        }
    )
