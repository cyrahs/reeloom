from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum

from reeloom.server.errors import ServerError, ServerErrorCode


class Role(IntEnum):
    VIEWER = 1
    OPERATOR = 2
    ADMIN = 3


@dataclass(frozen=True, slots=True)
class AuthSettings:
    _credential_hashes: tuple[tuple[Role, bytes], ...]
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]

    @classmethod
    def create(
        cls,
        *,
        credentials: dict[Role, str],
        allowed_hosts: tuple[str, ...],
        allowed_origins: tuple[str, ...],
    ) -> AuthSettings:
        if (
            set(credentials) != set(Role)
            or not allowed_hosts
            or not allowed_origins
            or any(
                not isinstance(token, str)
                or not 16 <= len(token.encode("utf-8")) <= 4_096
                for token in credentials.values()
            )
            or len(set(credentials.values())) != len(Role)
        ):
            raise ServerError(ServerErrorCode.INVALID_SETTINGS)
        hashes = tuple(
            sorted(
                (
                    role,
                    hashlib.sha256(token.encode("utf-8")).digest(),
                )
                for role, token in credentials.items()
            )
        )
        return cls(
            _credential_hashes=hashes,
            allowed_hosts=frozenset(item.lower() for item in allowed_hosts),
            allowed_origins=frozenset(allowed_origins),
        )

    def authenticate(self, token: str) -> Role | None:
        if (
            not isinstance(token, str)
            or not 1 <= len(token.encode("utf-8")) <= 4_096
        ):
            return None
        supplied = hashlib.sha256(token.encode("utf-8")).digest()
        matched: Role | None = None
        for role, expected in self._credential_hashes:
            if hmac.compare_digest(supplied, expected):
                matched = role
        return matched

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> AuthSettings:
        source = os.environ if environ is None else environ
        try:
            credentials = {
                Role.ADMIN: source["REELOOM_ADMIN_TOKEN"],
                Role.OPERATOR: source["REELOOM_OPERATOR_TOKEN"],
                Role.VIEWER: source["REELOOM_VIEWER_TOKEN"],
            }
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
            credentials=credentials,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )

    def __repr__(self) -> str:
        return (
            "AuthSettings(_credential_hashes=<redacted>, "
            f"allowed_hosts={self.allowed_hosts!r}, "
            f"allowed_origins={self.allowed_origins!r})"
        )
