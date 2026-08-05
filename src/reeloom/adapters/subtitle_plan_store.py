from __future__ import annotations

import os
from dataclasses import dataclass

from reeloom.adapters._immutable_file import (
    ImmutableFileError,
    ImmutableFileErrorCode,
    open_root,
    read_at,
    write_once_at,
)
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.kernel.rename_plan import is_valid_plan_hash
from reeloom.kernel.subtitle_acquisition import (
    SubtitleAcquisitionPlan,
    verify_subtitle_acquisition_plan_bytes,
)
from reeloom.policy.path_policy import AuthorizedRoot

_MAX_PLAN_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FilesystemSubtitleAcquisitionPlanStore:
    """Write-once store with a namespace separate from media rename plans."""

    root: AuthorizedRoot

    def save(self, plan: SubtitleAcquisitionPlan) -> None:
        if not isinstance(plan, SubtitleAcquisitionPlan) or not plan.verify_hash():
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        content = plan.canonical_bytes()
        if not 0 < len(content) <= _MAX_PLAN_BYTES:
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        root_fd = self._open_root()
        try:
            write_once_at(
                root_fd,
                self._name(plan.plan_hash),
                content,
                limit=_MAX_PLAN_BYTES,
            )
        except ImmutableFileError as error:
            if error.code is ImmutableFileErrorCode.EXISTS:
                raise ExecutorError(
                    ExecutorErrorCode.PLAN_ALREADY_EXISTS
                ) from None
            raise ExecutorError(ExecutorErrorCode.PLAN_STORE_FAILURE) from None
        finally:
            os.close(root_fd)

    def load(self, plan_hash: str) -> bytes:
        root_fd = self._open_root()
        try:
            content = read_at(
                root_fd,
                self._name(plan_hash),
                limit=_MAX_PLAN_BYTES,
            )
            if not verify_subtitle_acquisition_plan_bytes(content, plan_hash):
                raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
            return content
        except ImmutableFileError as error:
            if error.code is ImmutableFileErrorCode.NOT_FOUND:
                raise ExecutorError(ExecutorErrorCode.PLAN_NOT_FOUND) from None
            if error.code is ImmutableFileErrorCode.INVALID:
                raise ExecutorError(ExecutorErrorCode.INVALID_PLAN) from None
            raise ExecutorError(ExecutorErrorCode.PLAN_STORE_FAILURE) from None
        finally:
            os.close(root_fd)

    def _open_root(self) -> int:
        try:
            return open_root(self.root)
        except ImmutableFileError:
            raise ExecutorError(ExecutorErrorCode.PLAN_STORE_FAILURE) from None

    @staticmethod
    def _name(plan_hash: object) -> str:
        if not is_valid_plan_hash(plan_hash):
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        return (
            "subtitle-acquisition-v1-"
            f"{plan_hash.removeprefix('sha256:')}.json"
        )
