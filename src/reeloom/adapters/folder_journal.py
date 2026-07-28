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
from reeloom.executor.folder_transaction import FolderTransactionRecord
from reeloom.policy.path_policy import AuthorizedRoot

_MAX_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class FilesystemFolderJournalStore:
    root: AuthorizedRoot

    @contextmanager
    def transaction_lock(
        self, transaction: FolderTransactionRecord
    ) -> Iterator[None]:
        root_fd = self._open_root()
        lock_fd: int | None = None
        try:
            lock_fd = os.open(
                f"{transaction.transaction_id}.lock",
                os.O_RDWR
                | os.O_CREAT
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=root_fd,
            )
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ExecutorError(
                        ExecutorErrorCode.TRANSACTION_BUSY
                    ) from None
                raise
            yield
        except ExecutorError:
            raise
        except OSError:
            raise ExecutorError(ExecutorErrorCode.JOURNAL_FAILURE) from None
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(root_fd)

    def begin(self, transaction: FolderTransactionRecord) -> None:
        self._write(
            f"{transaction.transaction_id}.journal.json",
            transaction.canonical_bytes(),
        )

    def record_started(self, transaction: FolderTransactionRecord) -> None:
        self._event(transaction, "folder_rename_started", "started")

    def record_renamed(self, transaction: FolderTransactionRecord) -> None:
        self._event(transaction, "folder_renamed", "renamed")

    def record_completed(self, transaction: FolderTransactionRecord) -> None:
        self._event(transaction, "folder_completed", "completed")

    def record_rolled_back(
        self, transaction: FolderTransactionRecord
    ) -> None:
        self._event(transaction, "folder_rolled_back", "rolled-back")

    def is_started(self, transaction: FolderTransactionRecord) -> bool:
        return self._has(transaction, "folder_rename_started", "started")

    def is_renamed(self, transaction: FolderTransactionRecord) -> bool:
        return self._has(transaction, "folder_renamed", "renamed")

    def is_completed(self, transaction: FolderTransactionRecord) -> bool:
        return self._has(transaction, "folder_completed", "completed")

    def is_rolled_back(self, transaction: FolderTransactionRecord) -> bool:
        return self._has(
            transaction,
            "folder_rolled_back",
            "rolled-back",
        )

    def _event(
        self,
        transaction: FolderTransactionRecord,
        event_type: str,
        label: str,
    ) -> None:
        self._write(
            f"{transaction.transaction_id}.{label}.json",
            transaction.event_bytes(event_type),
        )

    def _has(
        self,
        transaction: FolderTransactionRecord,
        event_type: str,
        label: str,
    ) -> bool:
        expected = transaction.event_bytes(event_type)
        try:
            actual = self._read(
                f"{transaction.transaction_id}.{label}.json"
            )
        except ExecutorError as error:
            if error.code is ExecutorErrorCode.JOURNAL_NOT_FOUND:
                return False
            raise
        if not hmac.compare_digest(actual, expected):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return True

    def _write(self, name: str, content: bytes) -> None:
        root_fd = self._open_root()
        try:
            write_once_at(
                root_fd,
                name,
                content,
                limit=_MAX_BYTES,
            )
        except ImmutableFileError as error:
            if error.code is ImmutableFileErrorCode.EXISTS:
                if not hmac.compare_digest(self._read(name), content):
                    raise ExecutorError(
                        ExecutorErrorCode.INVALID_JOURNAL
                    ) from None
                return
            raise ExecutorError(ExecutorErrorCode.JOURNAL_FAILURE) from None
        finally:
            os.close(root_fd)

    def _read(self, name: str) -> bytes:
        root_fd = self._open_root()
        try:
            return read_at(root_fd, name, limit=_MAX_BYTES)
        except ImmutableFileError as error:
            if error.code is ImmutableFileErrorCode.NOT_FOUND:
                raise ExecutorError(
                    ExecutorErrorCode.JOURNAL_NOT_FOUND
                ) from None
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL) from None
        finally:
            os.close(root_fd)

    def _open_root(self) -> int:
        try:
            return open_root(self.root)
        except ImmutableFileError:
            raise ExecutorError(ExecutorErrorCode.JOURNAL_FAILURE) from None
