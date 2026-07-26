from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.database import PostgresControlPlane
from reeloom.server.errors import ServerError, ServerErrorCode


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


@pytest.mark.postgres
def test_config_cas_has_one_winner_and_preserves_history(
    tmp_path: Path,
) -> None:
    del tmp_path
    control = PostgresControlPlane(_dsn())
    try:
        control.open()
        control.migrate()
        repository = PostgresConfigRepository(control.pool)
        head = repository.head()
        expected = 0 if head is None else head.revision
        draft = ConfigDraft(
            watches=(),
            archive_routes=(),
            provider=ProviderConfig(
                base_url="https://api.openai.com/v1",
                model="gpt-5",
                secret_ref="secret-test",
            ),
            apply_policy=ApplyPolicy.MANUAL,
        )
        candidates = tuple(
            ConfigRevision.create(
                revision_id=uuid.uuid4().hex,
                revision=expected + 1,
                created_at=datetime.now(UTC),
                draft=draft,
            )
            for _ in range(2)
        )

        def append(item: ConfigRevision) -> object:
            try:
                return repository.compare_and_append(
                    expected_revision=expected,
                    revision=item,
                )
            except ServerError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(append, candidates))

        assert sum(
            isinstance(item, ConfigRevision) for item in results
        ) == 1
        assert results.count(ServerErrorCode.CONFIG_CONFLICT) == 1
        stored = repository.get(expected + 1)
        assert repository.head() == stored
    finally:
        control.close()
