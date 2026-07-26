from __future__ import annotations

import errno
import fcntl
import hmac
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from reeloom.adapters._immutable_file import (
    ImmutableFileError,
    ImmutableFileErrorCode,
    open_root,
    read_at,
    write_once_at,
)
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.transaction import (
    TransactionRecord,
    journal_event_bytes,
)
from reeloom.kernel.candidates import CandidateId
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.journals import JournalTerminalSummary

_MAX_JOURNAL_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FilesystemJournalStore:
    """Append-only transaction metadata stored as immutable files."""

    root: AuthorizedRoot

    @contextmanager
    def transaction_lock(
        self,
        transaction: TransactionRecord,
    ) -> Iterator[None]:
        root_fd = self._open_root()
        lock_fd: int | None = None
        try:
            lock_fd = os.open(
                self._lock_name(transaction),
                self._lock_flags(),
                0o600,
                dir_fd=root_fd,
            )
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ExecutorError(
                    ExecutorErrorCode.INVALID_JOURNAL
                )
            try:
                fcntl.flock(
                    lock_fd,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise ExecutorError(
                        ExecutorErrorCode.TRANSACTION_BUSY
                    ) from None
                raise
            yield
        except ExecutorError:
            raise
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.JOURNAL_FAILURE
            ) from None
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(root_fd)

    def begin(self, transaction: TransactionRecord) -> None:
        self._write_once(
            self._journal_name(transaction),
            transaction.canonical_bytes(),
        )

    def require(self, transaction: TransactionRecord) -> None:
        expected = transaction.canonical_bytes()
        actual = self._read(self._journal_name(transaction))
        if not hmac.compare_digest(actual, expected):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)

    def record_move(
        self,
        transaction: TransactionRecord,
        candidate_id: CandidateId,
    ) -> None:
        self._record_candidate_event(
            transaction,
            candidate_id,
            event_type="move_applied",
            label="move",
        )

    def record_rollback(
        self,
        transaction: TransactionRecord,
        candidate_id: CandidateId,
    ) -> None:
        self._record_candidate_event(
            transaction,
            candidate_id,
            event_type="move_rolled_back",
            label="rollback",
        )

    def record_rollback_started(
        self,
        transaction: TransactionRecord,
        candidate_id: CandidateId,
    ) -> None:
        self._record_candidate_event(
            transaction,
            candidate_id,
            event_type="rollback_started",
            label="rollback-started",
        )

    def record_failure(
        self,
        transaction: TransactionRecord,
        code: ExecutorErrorCode,
    ) -> None:
        self._write_event(
            transaction,
            f"failed-{code.value.replace('_', '-')}",
            journal_event_bytes(
                transaction,
                event_type="apply_failed",
                failure_code=code.value,
            ),
        )

    def record_completed(self, transaction: TransactionRecord) -> None:
        self._write_event(
            transaction,
            "completed",
            journal_event_bytes(
                transaction,
                event_type="apply_completed",
            ),
        )

    def record_rolled_back(
        self,
        transaction: TransactionRecord,
    ) -> None:
        self._write_event(
            transaction,
            "rolled-back",
            journal_event_bytes(
                transaction,
                event_type="apply_rolled_back",
            ),
        )

    def is_completed(self, transaction: TransactionRecord) -> bool:
        return self._has_event(
            transaction,
            label="completed",
            event_type="apply_completed",
        )

    def is_rolled_back(self, transaction: TransactionRecord) -> bool:
        return self._has_event(
            transaction,
            label="rolled-back",
            event_type="apply_rolled_back",
        )

    def is_rollback_started(
        self,
        transaction: TransactionRecord,
        candidate_id: CandidateId,
    ) -> bool:
        candidate_label = str(candidate_id).replace(":", "-")
        return self._has_event(
            transaction,
            label=f"rollback-started-{candidate_label}",
            event_type="rollback_started",
            candidate_id=str(candidate_id),
        )

    def terminal_summary(
        self,
        transaction: TransactionRecord,
        candidate_ids: tuple[CandidateId, ...],
    ) -> JournalTerminalSummary:
        if (
            not isinstance(candidate_ids, tuple)
            or len(candidate_ids) > 10_000
            or len(set(candidate_ids)) != len(candidate_ids)
            or not all(
                isinstance(item, CandidateId)
                for item in candidate_ids
            )
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        completed = self.is_completed(transaction)
        rolled_back = self.is_rolled_back(transaction)
        if completed and rolled_back:
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        moved: set[CandidateId] = set()
        restored: set[CandidateId] = set()
        for candidate_id in candidate_ids:
            label = str(candidate_id).replace(":", "-")
            if self._has_event(
                transaction,
                label=f"move-{label}",
                event_type="move_applied",
                candidate_id=str(candidate_id),
            ):
                moved.add(candidate_id)
            if self._has_event(
                transaction,
                label=f"rollback-{label}",
                event_type="move_rolled_back",
                candidate_id=str(candidate_id),
            ):
                restored.add(candidate_id)
        failures = tuple(
            code
            for code in ExecutorErrorCode
            if self._has_event(
                transaction,
                label=f"failed-{code.value.replace('_', '-')}",
                event_type="apply_failed",
                failure_code=code.value,
            )
        )
        if len(failures) > 1:
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        applied = moved | restored
        if completed and (
            applied != set(candidate_ids)
            or restored
            or failures
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return JournalTerminalSummary(
            completed=completed,
            rolled_back=rolled_back,
            applied_count=len(applied),
            rolled_back_count=(
                len(applied) if rolled_back else 0
            ),
            failure_code=failures[0] if failures else None,
        )

    def _has_event(
        self,
        transaction: TransactionRecord,
        *,
        label: str,
        event_type: str,
        candidate_id: str | None = None,
        failure_code: str | None = None,
    ) -> bool:
        name = self._event_name(transaction, label)
        expected = journal_event_bytes(
            transaction,
            event_type=event_type,
            candidate_id=candidate_id,
            failure_code=failure_code,
        )
        try:
            actual = self._read(name)
        except ExecutorError as error:
            if error.code is ExecutorErrorCode.JOURNAL_NOT_FOUND:
                return False
            raise
        if not hmac.compare_digest(actual, expected):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return True

    def _record_candidate_event(
        self,
        transaction: TransactionRecord,
        candidate_id: CandidateId,
        *,
        event_type: str,
        label: str,
    ) -> None:
        if not isinstance(candidate_id, CandidateId):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        candidate_label = str(candidate_id).replace(":", "-")
        self._write_event(
            transaction,
            f"{label}-{candidate_label}",
            journal_event_bytes(
                transaction,
                event_type=event_type,
                candidate_id=str(candidate_id),
            ),
        )

    def _write_event(
        self,
        transaction: TransactionRecord,
        label: str,
        content: bytes,
    ) -> None:
        self._write_once(
            self._event_name(transaction, label),
            content,
        )

    def _write_once(
        self,
        name: str,
        content: bytes,
    ) -> None:
        root_fd = self._open_root()
        try:
            write_once_at(
                root_fd,
                name,
                content,
                limit=_MAX_JOURNAL_BYTES,
            )
        except ImmutableFileError as error:
            if error.code is ImmutableFileErrorCode.EXISTS:
                if not hmac.compare_digest(self._read(name), content):
                    raise ExecutorError(
                        ExecutorErrorCode.INVALID_JOURNAL
                    ) from None
                return
            if error.code is ImmutableFileErrorCode.INVALID:
                raise ExecutorError(
                    ExecutorErrorCode.INVALID_JOURNAL
                ) from None
            raise ExecutorError(
                ExecutorErrorCode.JOURNAL_FAILURE
            ) from None
        finally:
            os.close(root_fd)

    def _read(self, name: str) -> bytes:
        root_fd = self._open_root()
        try:
            return read_at(
                root_fd,
                name,
                limit=_MAX_JOURNAL_BYTES,
            )
        except ImmutableFileError as error:
            if error.code is ImmutableFileErrorCode.NOT_FOUND:
                raise ExecutorError(
                    ExecutorErrorCode.JOURNAL_NOT_FOUND
                ) from None
            if error.code is ImmutableFileErrorCode.INVALID:
                raise ExecutorError(
                    ExecutorErrorCode.INVALID_JOURNAL
                ) from None
            raise ExecutorError(
                ExecutorErrorCode.JOURNAL_FAILURE
            ) from None
        finally:
            os.close(root_fd)

    def _open_root(self) -> int:
        try:
            return open_root(self.root)
        except ImmutableFileError:
            raise ExecutorError(
                ExecutorErrorCode.JOURNAL_FAILURE
            ) from None

    @staticmethod
    def _journal_name(transaction: TransactionRecord) -> str:
        if not TransactionRecord.is_valid_id(
            transaction.transaction_id
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return f"{transaction.transaction_id}.journal.json"

    @staticmethod
    def _event_name(
        transaction: TransactionRecord,
        label: str,
    ) -> str:
        if (
            not TransactionRecord.is_valid_id(
                transaction.transaction_id
            )
            or not label
            or any(
                not (
                    character.isascii()
                    and (
                        character.isalnum()
                        or character == "-"
                    )
                )
                for character in label
            )
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return f"{transaction.transaction_id}.{label}.json"

    @staticmethod
    def _lock_name(transaction: TransactionRecord) -> str:
        if not TransactionRecord.is_valid_id(
            transaction.transaction_id
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return f"{transaction.transaction_id}.lock"

    @staticmethod
    def _lock_flags() -> int:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise ExecutorError(ExecutorErrorCode.JOURNAL_FAILURE)
        return (
            os.O_RDWR
            | os.O_CREAT
            | no_follow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
