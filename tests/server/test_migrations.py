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
    assert "service_boots" in migrations[0].sql
    assert "schema_migrations" in migrations[0].sql
    assert "subtitle_successor_outbox" in migrations[-3].sql
    assert "subtitle_acquisition_lineages" in migrations[-3].sql
    assert "superseded" in migrations[-3].sql
    assert "subtitle_acquisition_requests" in migrations[-2].sql
    assert "subtitle_acquire" in migrations[-2].sql
    assert "expected_event_sequence" in migrations[-1].sql


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
