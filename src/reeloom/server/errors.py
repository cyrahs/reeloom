from __future__ import annotations

from enum import StrEnum


class ServerErrorCode(StrEnum):
    INVALID_SETTINGS = "invalid_settings"
    MULTIPLE_WORKERS = "multiple_workers"
    DATABASE_UNAVAILABLE = "database_unavailable"
    DATABASE_VERSION_MISMATCH = "database_version_mismatch"
    SCHEMA_MISMATCH = "schema_mismatch"
    MIGRATION_CHECKSUM_DRIFT = "migration_checksum_drift"
    INSTANCE_ALREADY_RUNNING = "instance_already_running"
    UNSAFE_STATE_ROOT = "unsafe_state_root"
    INVALID_CONFIG = "invalid_config"
    CONFIG_CONFLICT = "config_conflict"
    CONFIG_NOT_FOUND = "config_not_found"
    INVALID_SECRET = "invalid_secret"
    SECRET_NOT_FOUND = "secret_not_found"
    SECRET_STORE_FAILURE = "secret_store_failure"
    PROVIDER_ORIGIN_REJECTED = "provider_origin_rejected"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    WATCH_NOT_FOUND = "watch_not_found"
    STALE_WATCH_SCAN = "stale_watch_scan"
    DISCOVERY_NOT_FOUND = "discovery_not_found"
    JOB_NOT_FOUND = "job_not_found"
    INTERACTION_NOT_FOUND = "interaction_not_found"
    INTERACTION_CONFLICT = "interaction_conflict"
    INTERACTION_INVALID_RESULT = "interaction_invalid_result"
    FRESH_MAPPING_REQUIRED = "fresh_mapping_required"
    RUN_BUSY = "run_busy"


class ServerError(RuntimeError):
    """A bounded server failure that never embeds credentials or SQL."""

    def __init__(self, code: ServerErrorCode) -> None:
        self.code = code
        super().__init__(code.value)
