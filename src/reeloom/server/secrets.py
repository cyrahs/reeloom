from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass

from reeloom.adapters._immutable_file import (
    ImmutableFileError,
    ImmutableFileErrorCode,
    open_root,
    read_at,
    write_once_at,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.errors import ServerError, ServerErrorCode

_MAX_SECRET_BYTES = 4_096
_REFERENCE = re.compile(r"^secret-v1-[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class FilesystemSecretStore:
    root: AuthorizedRoot

    def put(self, value: bytes) -> str:
        if not isinstance(value, bytes) or not 0 < len(value) <= _MAX_SECRET_BYTES:
            raise ServerError(ServerErrorCode.INVALID_SECRET)
        root_descriptor = self._open()
        try:
            for _ in range(4):
                reference = f"secret-v1-{secrets.token_hex(16)}"
                try:
                    write_once_at(
                        root_descriptor,
                        reference,
                        value,
                        limit=_MAX_SECRET_BYTES,
                    )
                    return reference
                except ImmutableFileError as error:
                    if error.code is ImmutableFileErrorCode.EXISTS:
                        continue
                    raise ServerError(
                        ServerErrorCode.SECRET_STORE_FAILURE
                    ) from None
            raise ServerError(ServerErrorCode.SECRET_STORE_FAILURE)
        finally:
            os.close(root_descriptor)

    def load(self, reference: str) -> bytes:
        if (
            not isinstance(reference, str)
            or _REFERENCE.fullmatch(reference) is None
        ):
            raise ServerError(ServerErrorCode.INVALID_SECRET)
        root_descriptor = self._open()
        try:
            return read_at(
                root_descriptor,
                reference,
                limit=_MAX_SECRET_BYTES,
            )
        except ImmutableFileError as error:
            if error.code in {
                ImmutableFileErrorCode.NOT_FOUND,
                ImmutableFileErrorCode.INVALID,
            }:
                raise ServerError(
                    ServerErrorCode.SECRET_NOT_FOUND
                ) from None
            raise ServerError(
                ServerErrorCode.SECRET_STORE_FAILURE
            ) from None
        finally:
            os.close(root_descriptor)

    def _open(self) -> int:
        try:
            return open_root(self.root)
        except ImmutableFileError:
            raise ServerError(
                ServerErrorCode.SECRET_STORE_FAILURE
            ) from None
