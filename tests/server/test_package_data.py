from __future__ import annotations

from reeloom.server.migrations import EXPECTED_SCHEMA_VERSION, MIGRATIONS


def test_all_migration_sql_is_available_as_package_data() -> None:
    assert tuple(item.version for item in MIGRATIONS) == tuple(
        range(1, EXPECTED_SCHEMA_VERSION + 1)
    )
    assert all(item.sql.strip() for item in MIGRATIONS)
