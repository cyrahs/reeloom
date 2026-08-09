from __future__ import annotations

from pathlib import Path

import pytest

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.migrations import (
    EXPECTED_SCHEMA_VERSION,
    Migration,
    discover_migrations,
    validate_migration_history,
)


def test_foundation_migration_is_versioned_and_immutable() -> None:
    migrations = discover_migrations()

    assert tuple(item.version for item in migrations) == tuple(
        range(1, EXPECTED_SCHEMA_VERSION + 1)
    )
    assert all(len(item.checksum) == 64 for item in migrations)
    by_version = {item.version: item.sql for item in migrations}
    assert "service_boots" in migrations[0].sql
    assert "schema_migrations" in migrations[0].sql
    assert "subtitle_request_published_transaction" in by_version[40]
    assert "subtitle_request_pre_effect_transaction" in by_version[40]
    assert "subtitle_successor_outbox" in by_version[27]
    assert "subtitle_acquisition_lineages" in by_version[27]
    assert "superseded" in by_version[27]
    assert "subtitle_acquisition_requests" in by_version[28]
    assert "subtitle_acquire" in by_version[28]
    assert "expected_event_sequence" in by_version[29]
    assert "failure_diagnostic" in by_version[30]
    assert "stabilizing_inventory_id" in by_version[31]
    assert "semantic_v2" in by_version[32]
    assert "execution_operations_v2" in by_version[33]
    assert "lease_expires_at" in by_version[33]
    assert "execution_operation_results_v2" in by_version[34]
    assert "execution_rescan_outbox_v2" in by_version[34]
    assert "subtitle_publication_settlements_v2" in by_version[35]
    assert "subtitle_scan_requests_v2" in by_version[35]
    assert "handled_folder_inventories_v2" in by_version[36]
    assert "folder_housekeeping_v2" in by_version[36]
    assert "legacy_effect_supersessions_v2" in by_version[37]
    assert "legacy_v1_superseded" in by_version[37]
    assert "watch_folder_observations_check" in by_version[38]
    assert "folder-inventory-v2:" in by_version[38]


def test_checksum_drift_fails_closed() -> None:
    migration = Migration(version=1, name="foundation", sql="SELECT 1")

    with pytest.raises(ServerError) as raised:
        validate_migration_history(
            migrations=(migration,),
            applied=((1, "0" * 64),),
        )

    assert raised.value.code is ServerErrorCode.MIGRATION_CHECKSUM_DRIFT


def test_schema_ahead_or_gapped_fails_closed() -> None:
    migrations = (Migration(version=1, name="foundation", sql="SELECT 1"),)

    for applied in (((2, migrations[0].checksum),), ((1, migrations[0].checksum), (3, "a" * 64))):
        with pytest.raises(ServerError) as raised:
            validate_migration_history(
                migrations=migrations,
                applied=applied,
            )
        assert raised.value.code is ServerErrorCode.SCHEMA_MISMATCH


def test_migration_discovery_does_not_accept_duplicate_versions(
    tmp_path: Path,
) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "0001_b.sql").write_text("SELECT 2", encoding="utf-8")

    with pytest.raises(ServerError) as raised:
        discover_migrations(tmp_path)

    assert raised.value.code is ServerErrorCode.SCHEMA_MISMATCH
