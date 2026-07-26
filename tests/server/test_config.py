from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from reeloom.server.config import (
    ApplyPolicy,
    ArchiveRoute,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
    ServerWorkType,
    WatchConfig,
)
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.provider import ProviderOriginPolicy


def _draft(tmp_path: Path) -> ConfigDraft:
    watch = tmp_path / "watch"
    archive = tmp_path / "archive"
    watch.mkdir()
    archive.mkdir()
    return ConfigDraft(
        watches=(
            WatchConfig(
                watch_id="watch-1",
                root=watch,
                work_type=ServerWorkType.ANIME,
                poll_interval_seconds=30,
                settle_interval_seconds=120,
            ),
        ),
        archive_routes=(
            ArchiveRoute(
                work_type=ServerWorkType.ANIME,
                root=archive,
            ),
        ),
        provider=ProviderConfig(
            base_url="https://models.example.test/v1",
            model="gpt-5",
            reasoning_effort="high",
            verbosity="low",
            secret_ref="secret-abc",
        ),
        apply_policy=ApplyPolicy.MANUAL,
    )


def test_config_is_canonical_versioned_and_round_trips(
    tmp_path: Path,
) -> None:
    draft = _draft(tmp_path)
    revision = ConfigRevision.create(
        revision_id="cfg-1",
        revision=1,
        created_at=datetime(2026, 7, 25, tzinfo=UTC),
        draft=draft,
    )

    restored = ConfigRevision.from_json(revision.to_json())

    assert restored == revision
    assert restored.revision == 1
    assert restored.provider.secret_ref == "secret-abc"
    public = restored.public_payload()
    assert public["revision"] == 1
    assert "secret-abc" not in repr(public)
    assert str(draft.watches[0].root) not in repr(public)


def test_config_requires_exact_archive_route_and_distinct_roots(
    tmp_path: Path,
) -> None:
    draft = _draft(tmp_path)

    with pytest.raises(ServerError) as raised:
        ConfigDraft(
            watches=draft.watches,
            archive_routes=(),
            provider=draft.provider,
            apply_policy=draft.apply_policy,
        )
    assert raised.value.code is ServerErrorCode.INVALID_CONFIG


@pytest.mark.parametrize(
    "url",
    [
        "http://models.example.test/v1",
        "https://user@models.example.test/v1",
        "https://models.example.test/v1?key=secret",
        "https://other.example.test/v1",
    ],
)
def test_provider_origin_must_match_deployment_allowlist(
    url: str,
) -> None:
    policy = ProviderOriginPolicy.create(
        ("https://models.example.test",)
    )

    with pytest.raises(ServerError) as raised:
        policy.validate_base_url(url)

    assert raised.value.code is ServerErrorCode.PROVIDER_ORIGIN_REJECTED


def test_provider_transport_pins_dns_and_preserves_host() -> None:
    import asyncio
    import httpx

    from reeloom.server.provider import _PinnedOriginTransport

    async def scenario() -> None:
        observed: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(200, request=request)

        def resolver(
            *args: object,
            **kwargs: object,
        ) -> list[tuple[object, ...]]:
            del args, kwargs
            return [(2, 1, 6, "", ("203.0.113.10", 443))]

        transport = _PinnedOriginTransport(
            "https://models.example.test/v1",
            resolver=resolver,
            transport=httpx.MockTransport(handler),
        )
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.get(
                    "https://models.example.test/v1/models"
                )
            assert response.status_code == 200
            assert observed[0].url.host == "203.0.113.10"
            assert observed[0].headers["host"] == "models.example.test"
            assert (
                observed[0].extensions["sni_hostname"]
                == "models.example.test"
            )
        finally:
            await transport.aclose()

    asyncio.run(scenario())


def test_provider_transport_bounds_total_response_and_deadline() -> None:
    import asyncio
    import httpx

    from reeloom.server.provider import _PinnedOriginTransport

    def resolver(
        *args: object,
        **kwargs: object,
    ) -> list[tuple[object, ...]]:
        del args, kwargs
        return [(2, 1, 6, "", ("203.0.113.10", 443))]

    async def scenario() -> None:
        oversized = _PinnedOriginTransport(
            "https://models.example.test/v1",
            resolver=resolver,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=b"12345", request=request
                )
            ),
            max_response_bytes=4,
        )
        try:
            async with httpx.AsyncClient(
                transport=oversized
            ) as client:
                with pytest.raises(httpx.DecodingError):
                    await client.get(
                        "https://models.example.test/v1/models"
                    )
        finally:
            await oversized.aclose()

        async def delayed(
            request: httpx.Request,
        ) -> httpx.Response:
            await asyncio.sleep(0.05)
            return httpx.Response(200, request=request)

        expiring = _PinnedOriginTransport(
            "https://models.example.test/v1",
            resolver=resolver,
            transport=httpx.MockTransport(delayed),
            total_timeout_seconds=0.01,
        )
        try:
            async with httpx.AsyncClient(
                transport=expiring
            ) as client:
                with pytest.raises(httpx.ReadTimeout):
                    await client.get(
                        "https://models.example.test/v1/models"
                    )
        finally:
            await expiring.aclose()

    asyncio.run(scenario())
