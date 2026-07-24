from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from reeloom.executor.errors import ExecutorErrorCode
from reeloom.executor.transaction import TransactionRecord
from reeloom.kernel.candidates import CandidateId


class JournalStore(Protocol):
    def transaction_lock(
        self,
        transaction: TransactionRecord,
    ) -> AbstractContextManager[None]: ...

    def begin(self, transaction: TransactionRecord) -> None: ...

    def require(self, transaction: TransactionRecord) -> None: ...

    def record_move(
        self,
        transaction: TransactionRecord,
        candidate_id: CandidateId,
    ) -> None: ...

    def record_rollback(
        self,
        transaction: TransactionRecord,
        candidate_id: CandidateId,
    ) -> None: ...

    def record_rollback_started(
        self,
        transaction: TransactionRecord,
        candidate_id: CandidateId,
    ) -> None: ...

    def record_failure(
        self,
        transaction: TransactionRecord,
        code: ExecutorErrorCode,
    ) -> None: ...

    def record_completed(self, transaction: TransactionRecord) -> None: ...

    def record_rolled_back(
        self,
        transaction: TransactionRecord,
    ) -> None: ...

    def is_completed(self, transaction: TransactionRecord) -> bool: ...

    def is_rolled_back(self, transaction: TransactionRecord) -> bool: ...

    def is_rollback_started(
        self,
        transaction: TransactionRecord,
        candidate_id: CandidateId,
    ) -> bool: ...
