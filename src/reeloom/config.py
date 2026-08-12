"""Process configuration.

Only deployment-level facts live in the environment: where the database is,
who the admin is, and where scratch space goes. Everything operational
(watch roots, API keys, model choice) is edited through the UI and stored in
PostgreSQL.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from reeloom.models import ReeloomError

logger = logging.getLogger(__name__)

_MIN_TOKEN_LENGTH = 16

_LINK_SCHEMES = ("tcp://", "udp://")
"""Value shapes only Docker-link style injection produces, never an operator."""


class ConfigError(ReeloomError):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    admin_token: str
    work_dir: Path
    """Scratch space for subtitle downloads and extraction."""
    host: str = "0.0.0.0"
    port: int = 8080
    scan_interval_seconds: int = 30
    public_url: str = ""
    """Where a browser reaches the web UI; empty means links fall back to
    TMDB. Deployment-level because only the operator knows the ingress."""

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ

        database_url = env.get("REELOOM_DATABASE_URL", "").strip()
        if not database_url:
            raise ConfigError("missing_database_url")

        admin_token = env.get("REELOOM_ADMIN_TOKEN", "").strip()
        if len(admin_token) < _MIN_TOKEN_LENGTH:
            raise ConfigError("weak_admin_token", minimum=_MIN_TOKEN_LENGTH)

        work_dir = Path(env.get("REELOOM_WORK_DIR", "/var/lib/reeloom"))
        if not work_dir.is_absolute():
            raise ConfigError("work_dir_not_absolute", value=str(work_dir))

        # The listen pair is spelled REELOOM_LISTEN_* because Kubernetes and
        # Docker inject <SERVICE>_PORT, <SERVICE>_SERVICE_HOST and
        # <SERVICE>_SERVICE_PORT into every container. A Service named
        # `reeloom` would otherwise hand us REELOOM_PORT=tcp://10.43.0.1:80.
        return cls(
            database_url=database_url,
            admin_token=admin_token,
            work_dir=work_dir,
            host=env.get("REELOOM_LISTEN_HOST", "0.0.0.0"),
            port=_int(env, "REELOOM_LISTEN_PORT", 8080, 1, 65535),
            scan_interval_seconds=_int(
                env, "REELOOM_SCAN_INTERVAL_SECONDS", 30, 5, 3600
            ),
            public_url=env.get("REELOOM_PUBLIC_URL", "").strip().rstrip("/"),
        )


def _int(
    env: dict[str, str], key: str, default: int, low: int, high: int
) -> int:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    if raw.startswith(_LINK_SCHEMES):
        # Our names sit outside the injected namespace, so a value shaped like
        # this is someone else's variable landing on ours. Fall back rather
        # than crash; a typo still fails loudly below.
        logger.warning(
            "ignoring injected service-link value for %s: %s", key, raw
        )
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigError("invalid_integer", key=key, value=raw) from error
    if not low <= value <= high:
        raise ConfigError("out_of_range", key=key, value=value)
    return value
