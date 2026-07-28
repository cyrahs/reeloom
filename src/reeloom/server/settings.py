from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from reeloom.server.errors import ServerError, ServerErrorCode


@dataclass(frozen=True, slots=True)
class DeploymentSettings:
    postgres_dsn: str
    state_root: Path
    workers: int = 1
    provider_origins: tuple[str, ...] = ("https://api.openai.com",)
    tmdb_api_key: str = ""

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> DeploymentSettings:
        source = os.environ if environ is None else environ
        dsn = source.get("REELOOM_POSTGRES_DSN", "")
        root_text = source.get("REELOOM_STATE_ROOT", "")
        worker_text = source.get("REELOOM_WORKERS", "1")
        origins_text = source.get(
            "REELOOM_PROVIDER_ORIGINS",
            "https://api.openai.com",
        )
        tmdb_api_key = source.get("REELOOM_TMDB_API_KEY", "")
        if (
            not isinstance(dsn, str)
            or not dsn.strip()
            or len(dsn.encode("utf-8")) > 8_192
            or not isinstance(root_text, str)
            or not root_text
            or not isinstance(tmdb_api_key, str)
            or not tmdb_api_key
            or len(tmdb_api_key.encode("utf-8")) > 512
        ):
            raise ServerError(ServerErrorCode.INVALID_SETTINGS)
        try:
            workers = int(worker_text)
        except (TypeError, ValueError):
            raise ServerError(ServerErrorCode.INVALID_SETTINGS) from None
        if workers != 1:
            raise ServerError(ServerErrorCode.MULTIPLE_WORKERS)
        origins = tuple(item.strip() for item in origins_text.split(","))
        if (
            not origins
            or any(not item for item in origins)
            or len(origins) > 32
        ):
            raise ServerError(ServerErrorCode.INVALID_SETTINGS)
        root = Path(root_text)
        if not root.is_absolute():
            raise ServerError(ServerErrorCode.INVALID_SETTINGS)
        return cls(
            postgres_dsn=dsn,
            state_root=root,
            workers=workers,
            provider_origins=origins,
            tmdb_api_key=tmdb_api_key,
        )

    def __repr__(self) -> str:
        return (
            "DeploymentSettings(postgres_dsn=<redacted>, "
            f"state_root=<redacted>, workers={self.workers}, "
            f"provider_origins={self.provider_origins!r}, "
            "tmdb_api_key=<redacted>)"
        )
