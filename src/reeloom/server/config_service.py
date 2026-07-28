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
from reeloom.server.provider import validate_provider_base_url


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
        clock: Callable[[], datetime] = _now,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._configs = configs
        self._secrets = secrets
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
        validate_provider_base_url(value.provider.base_url)
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
        return self.compare_and_append_draft(
            expected_revision=expected_revision,
            draft=draft,
        )

    def compare_and_append_draft(
        self,
        *,
        expected_revision: int,
        draft: ConfigDraft,
        replacement_api_key: bytes | None = None,
    ) -> ConfigRevision:
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or not isinstance(draft, ConfigDraft)
            or (
                replacement_api_key is not None
                and (
                    not isinstance(replacement_api_key, bytes)
                    or not 0 < len(replacement_api_key) <= 4_096
                )
            )
        ):
            from reeloom.server.errors import ServerError, ServerErrorCode

            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        validate_provider_base_url(draft.provider.base_url)
        if replacement_api_key is not None:
            secret_ref = self._secrets.put(replacement_api_key)
            draft = ConfigDraft(
                watches=draft.watches,
                archive_routes=draft.archive_routes,
                provider=ProviderConfig(
                    base_url=draft.provider.base_url,
                    model=draft.provider.model,
                    reasoning_effort=draft.provider.reasoning_effort,
                    verbosity=draft.provider.verbosity,
                    secret_ref=secret_ref,
                ),
                apply_policy=draft.apply_policy,
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
