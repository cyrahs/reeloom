from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from reeloom.server.errors import ServerError, ServerErrorCode

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,4096}")


@dataclass(frozen=True, slots=True)
class AuthSettings:
    _credential_hash: bytes
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]

    @classmethod
    def create(
        cls,
        *,
        admin_token: str,
        allowed_hosts: tuple[str, ...],
        allowed_origins: tuple[str, ...],
    ) -> AuthSettings:
        if (
            not isinstance(admin_token, str)
            or _TOKEN_PATTERN.fullmatch(admin_token) is None
            or not allowed_hosts
            or not allowed_origins
        ):
            raise ServerError(ServerErrorCode.INVALID_SETTINGS)
        return cls(
            _credential_hash=hashlib.sha256(
                admin_token.encode("ascii")
            ).digest(),
            allowed_hosts=frozenset(item.lower() for item in allowed_hosts),
            allowed_origins=frozenset(allowed_origins),
        )

    def authenticate(self, token: str) -> bool:
        if (
            not isinstance(token, str)
            or _TOKEN_PATTERN.fullmatch(token) is None
        ):
            return False
        supplied = hashlib.sha256(token.encode("ascii")).digest()
        return hmac.compare_digest(supplied, self._credential_hash)

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> AuthSettings:
        source = os.environ if environ is None else environ
        try:
            admin_token = source["REELOOM_ADMIN_TOKEN"]
            hosts = tuple(
                item.strip()
                for item in source["REELOOM_ALLOWED_HOSTS"].split(",")
                if item.strip()
            )
            origins = tuple(
                item.strip()
                for item in source["REELOOM_ALLOWED_UI_ORIGINS"].split(",")
                if item.strip()
            )
        except KeyError:
            raise ServerError(ServerErrorCode.INVALID_SETTINGS) from None
        return cls.create(
            admin_token=admin_token,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )

    def __repr__(self) -> str:
        return (
            "AuthSettings(_credential_hash=<redacted>, "
            f"allowed_hosts={self.allowed_hosts!r}, "
            f"allowed_origins={self.allowed_origins!r})"
        )
