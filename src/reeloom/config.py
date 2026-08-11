"""Process configuration.

Only deployment-level facts live in the environment: where the database is,
who the admin is, and where scratch space goes. Everything operational
(watch roots, API keys, model choice) is edited through the UI and stored in
PostgreSQL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from reeloom.models import ReeloomError

_MIN_TOKEN_LENGTH = 16


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

        return cls(
            database_url=database_url,
            admin_token=admin_token,
            work_dir=work_dir,
            host=env.get("REELOOM_HOST", "0.0.0.0"),
            port=_int(env, "REELOOM_PORT", 8080, 1, 65535),
            scan_interval_seconds=_int(
                env, "REELOOM_SCAN_INTERVAL_SECONDS", 30, 5, 3600
            ),
        )


def _int(
    env: dict[str, str], key: str, default: int, low: int, high: int
) -> int:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigError("invalid_integer", key=key, value=raw) from error
    if not low <= value <= high:
        raise ConfigError("out_of_range", key=key, value=value)
    return value
