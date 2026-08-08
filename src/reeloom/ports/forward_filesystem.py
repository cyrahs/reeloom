from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol

from reeloom.kernel.forward_execution import PathObservationState
from reeloom.kernel.semantic_identity import (
    SemanticRootBinding,
    SemanticSourceIdentity,
)


class ForwardMoveDiagnostic(StrEnum):
    NATIVE = "native"
    CHECKED_RENAME = "checked_rename"
    COLLISION = "collision"
    CROSS_FILESYSTEM = "cross_filesystem"
    PERMISSION_DENIED = "permission_denied"
    TRANSIENT_IO = "transient_io"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ForwardMoveEffect:
    diagnostic: ForwardMoveDiagnostic
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.diagnostic, ForwardMoveDiagnostic)
            or not isinstance(self.warnings, tuple)
            or any(
                not isinstance(item, str) or not item
                for item in self.warnings
            )
        ):
            raise ValueError("invalid forward move effect")


class ForwardFilesystem(Protocol):
    def observe(
        self,
        *,
        root: SemanticRootBinding,
        relative_path: PurePosixPath,
        expected: SemanticSourceIdentity,
    ) -> PathObservationState: ...

    def move(
        self,
        *,
        source_root: SemanticRootBinding,
        source_path: PurePosixPath,
        expected: SemanticSourceIdentity,
        destination_root: SemanticRootBinding,
        destination_path: PurePosixPath,
    ) -> ForwardMoveEffect: ...
