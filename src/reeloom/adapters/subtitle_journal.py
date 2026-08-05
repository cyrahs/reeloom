from __future__ import annotations

import errno
import fcntl
import hmac
import json
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
from reeloom.executor.subtitle_transaction import (
    SubtitleAcquisitionTransactionRecord,
)
from reeloom.policy.path_policy import AuthorizedRoot

_MAX_RECORD_BYTES = 64 * 1024
_EVENT_LABELS = frozenset(
    {
        "approval_claimed",
        "downloads_verified",
        "staging_create_started",
        "publish_started",
        "published",
        "completed",
        "failed",
    }
)


@dataclass(frozen=True, slots=True)
class FilesystemSubtitleAcquisitionJournalStore:
    root: AuthorizedRoot

    @contextmanager
    def transaction_lock(
        self,
        transaction: SubtitleAcquisitionTransactionRecord,
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

    def begin(self, transaction: SubtitleAcquisitionTransactionRecord) -> None:
        self._write(
            f"{transaction.transaction_id}.journal.json",
            transaction.canonical_bytes(),
        )

    def record(
        self,
        transaction: SubtitleAcquisitionTransactionRecord,
        event_type: str,
        *,
        failure_code: str | None = None,
    ) -> None:
        self._write(
            self._event_name(transaction, event_type),
            transaction.event_bytes(
                event_type,
                failure_code=failure_code,
            ),
        )

    def has(
        self,
        transaction: SubtitleAcquisitionTransactionRecord,
        event_type: str,
        *,
        failure_code: str | None = None,
    ) -> bool:
        return self._has(
            self._event_name(transaction, event_type),
            transaction.event_bytes(
                event_type,
                failure_code=failure_code,
            ),
        )

    def record_member(
        self,
        transaction: SubtitleAcquisitionTransactionRecord,
        member_index: int,
    ) -> None:
        self._write(
            self._member_name(transaction, member_index),
            transaction.event_bytes(
                "member_written",
                member_index=member_index,
            ),
        )

    def has_member(
        self,
        transaction: SubtitleAcquisitionTransactionRecord,
        member_index: int,
    ) -> bool:
        return self._has(
            self._member_name(transaction, member_index),
            transaction.event_bytes(
                "member_written",
                member_index=member_index,
            ),
        )

    def record_staging(
        self,
        transaction: SubtitleAcquisitionTransactionRecord,
        *,
        device: int,
        inode: int,
    ) -> None:
        self._write(
            f"{transaction.transaction_id}.staging_created.json",
            transaction.event_bytes(
                "staging_created",
                staging_device=device,
                staging_inode=inode,
            ),
        )

    def staging_identity(
        self,
        transaction: SubtitleAcquisitionTransactionRecord,
    ) -> tuple[int, int] | None:
        name = f"{transaction.transaction_id}.staging_created.json"
        try:
            content = self._read(name)
        except ExecutorError as error:
            if error.code is ExecutorErrorCode.JOURNAL_NOT_FOUND:
                return None
            raise
        try:
            payload = json.loads(content)
            if not isinstance(payload, dict) or set(payload) != {
                "event_type",
                "failure_code",
                "member_index",
                "schema_version",
                "staging_device",
                "staging_inode",
                "transaction_id",
            }:
                raise ValueError
            device = payload["staging_device"]
            inode = payload["staging_inode"]
            expected = transaction.event_bytes(
                "staging_created",
                staging_device=device,
                staging_inode=inode,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL) from None
        if not hmac.compare_digest(content, expected):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return device, inode

    @staticmethod
    def _event_name(
        transaction: SubtitleAcquisitionTransactionRecord,
        event_type: str,
    ) -> str:
        if event_type not in _EVENT_LABELS:
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return f"{transaction.transaction_id}.{event_type}.json"

    @staticmethod
    def _member_name(
        transaction: SubtitleAcquisitionTransactionRecord,
        member_index: int,
    ) -> str:
        transaction.event_bytes("member_written", member_index=member_index)
        return (
            f"{transaction.transaction_id}.member-"
            f"{member_index:04d}.json"
        )

    def _has(self, name: str, expected: bytes) -> bool:
        try:
            actual = self._read(name)
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
                limit=_MAX_RECORD_BYTES,
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
            return read_at(root_fd, name, limit=_MAX_RECORD_BYTES)
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
