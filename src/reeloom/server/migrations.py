from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from reeloom.server.errors import ServerError, ServerErrorCode

_MIGRATION_PATTERN = re.compile(
    r"^(?P<version>[0-9]{4})_(?P<name>[a-z][a-z0-9_]*)\.sql$"
)
_MIGRATION_ROOT = Path(__file__).with_name("sql")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover_migrations(root: Path = _MIGRATION_ROOT) -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    seen: set[int] = set()
    try:
        entries = sorted(root.iterdir())
    except OSError:
        raise ServerError(ServerErrorCode.SCHEMA_MISMATCH) from None
    for path in entries:
        match = _MIGRATION_PATTERN.fullmatch(path.name)
        if match is None or not path.is_file():
            continue
        version = int(match.group("version"))
        if version in seen:
            raise ServerError(ServerErrorCode.SCHEMA_MISMATCH)
        seen.add(version)
        try:
            sql = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ServerError(ServerErrorCode.SCHEMA_MISMATCH) from None
        if not sql.strip():
            raise ServerError(ServerErrorCode.SCHEMA_MISMATCH)
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                sql=sql,
            )
        )
    if tuple(item.version for item in migrations) != tuple(
        range(1, len(migrations) + 1)
    ):
        raise ServerError(ServerErrorCode.SCHEMA_MISMATCH)
    return tuple(migrations)


MIGRATIONS = discover_migrations()
EXPECTED_SCHEMA_VERSION = len(MIGRATIONS)


def validate_migration_history(
    *,
    migrations: tuple[Migration, ...],
    applied: tuple[tuple[int, str], ...],
) -> int:
    versions = tuple(version for version, _ in applied)
    if versions != tuple(range(1, len(applied) + 1)):
        raise ServerError(ServerErrorCode.SCHEMA_MISMATCH)
    if len(applied) > len(migrations):
        raise ServerError(ServerErrorCode.SCHEMA_MISMATCH)
    for version, checksum in applied:
        migration = migrations[version - 1]
        if migration.checksum != checksum:
            raise ServerError(
                ServerErrorCode.MIGRATION_CHECKSUM_DRIFT
            )
    return len(applied)
