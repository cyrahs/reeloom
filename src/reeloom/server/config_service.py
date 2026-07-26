from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from reeloom.server.config import (
    ConfigDraft,
    ConfigDraftInput,
    ConfigRevision,
    ProviderConfig,
)
from reeloom.server.provider import ProviderOriginPolicy


class ConfigRepository(Protocol):
    def compare_and_append(
        self,
        *,
        expected_revision: int,
        revision: ConfigRevision,
    ) -> ConfigRevision: ...


class SecretWriter(Protocol):
    def put(self, value: bytes) -> str: ...


def _now() -> datetime:
    return datetime.now(UTC)


class ConfigService:
    def __init__(
        self,
        *,
        configs: ConfigRepository,
        secrets: SecretWriter,
        origins: ProviderOriginPolicy,
        clock: Callable[[], datetime] = _now,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._configs = configs
        self._secrets = secrets
        self._origins = origins
        self._clock = clock
        self._id_factory = id_factory

    def compare_and_append(
        self,
        *,
        expected_revision: int,
        value: ConfigDraftInput,
    ) -> ConfigRevision:
        if type(expected_revision) is not int or expected_revision < 0:
            from reeloom.server.errors import ServerError, ServerErrorCode

            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        self._origins.validate_base_url(value.provider.base_url)
        secret_ref = self._secrets.put(value.provider.api_key)
        draft = ConfigDraft(
            watches=value.watches,
            archive_routes=value.archive_routes,
            provider=ProviderConfig(
                base_url=value.provider.base_url,
                model=value.provider.model,
                reasoning_effort=value.provider.reasoning_effort,
                verbosity=value.provider.verbosity,
                secret_ref=secret_ref,
            ),
            apply_policy=value.apply_policy,
        )
        revision = ConfigRevision.create(
            revision_id=self._id_factory(),
            revision=expected_revision + 1,
            created_at=self._clock(),
            draft=draft,
        )
        return self._configs.compare_and_append(
            expected_revision=expected_revision,
            revision=revision,
        )
