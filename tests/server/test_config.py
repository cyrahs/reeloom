from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.runtime.budget import RunBudget
from reeloom.server.agent_worker import InitialAgentWorker
from reeloom.server.config import (
    ApplyPolicy,
    AcgripConfig,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
    ServerWorkType,
    TelegramConfig,
    SubtitleAcquisitionPolicy,
    WatchConfig,
)
from reeloom.server.notifications import NotificationType
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.provider import (
    ControlledModelLease,
    validate_provider_base_url,
)
from reeloom.server.scheduler import (
    AgentJobContext,
    Discovery,
    RunRegistration,
)


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
                library_root=archive,
                work_type=ServerWorkType.ANIME,
                poll_interval_seconds=30,
                settle_interval_seconds=120,
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
    assert public["watches"][0]["root"] == str(draft.watches[0].root)
    assert public["watches"][0]["library_root"] == str(
        draft.watches[0].library_root
    )
    assert public["agent_budget"]["max_elapsed_seconds"] == 600


def test_config_budget_is_strict_and_round_trips(tmp_path: Path) -> None:
    draft = _draft(tmp_path)
    revision = ConfigRevision.create(
        revision_id="cfg-budget",
        revision=2,
        created_at=datetime(2026, 7, 25, tzinfo=UTC),
        draft=ConfigDraft(
            watches=draft.watches,
            provider=draft.provider,
            apply_policy=draft.apply_policy,
            agent_budget=RunBudget(
                max_model_turns=32,
                max_tool_calls=48,
                max_failures=2,
                max_total_tokens=250_000,
                max_elapsed_seconds=900,
            ),
        ),
    )

    assert ConfigRevision.from_json(revision.to_json()) == revision
    payload = json.loads(revision.to_json())
    payload["agent_budget"]["unexpected"] = True
    with pytest.raises(ServerError) as raised:
        ConfigRevision.from_json(json.dumps(payload))
    assert raised.value.code is ServerErrorCode.INVALID_CONFIG


def test_config_rejects_source_library_overlap(
    tmp_path: Path,
) -> None:
    draft = _draft(tmp_path)
    watch = draft.watches[0]

    with pytest.raises(ServerError) as raised:
        ConfigDraft(
            watches=(
                WatchConfig(
                    watch_id=watch.watch_id,
                    root=watch.root,
                    library_root=watch.root,
                    work_type=watch.work_type,
                    poll_interval_seconds=watch.poll_interval_seconds,
                    settle_interval_seconds=watch.settle_interval_seconds,
                ),
            ),
            provider=draft.provider,
            apply_policy=draft.apply_policy,
        )
    assert raised.value.code is ServerErrorCode.INVALID_CONFIG


def test_legacy_config_maps_routes_to_each_watch(tmp_path: Path) -> None:
    watch_a = tmp_path / "watch-a"
    watch_b = tmp_path / "watch-b"
    library = tmp_path / "library"
    for path in (watch_a, watch_b, library):
        path.mkdir()
    payload = {
        "apply_policy": "manual",
        "archive_routes": [
            {"root": str(library), "work_type": "anime"},
        ],
        "created_at": "2026-07-25T00:00:00+00:00",
        "provider": {
            "base_url": "https://models.example.test/v1",
            "model": "gpt-5",
            "reasoning_effort": None,
            "secret_ref": "secret-abc",
            "verbosity": None,
        },
        "revision": 1,
        "revision_id": "cfg-legacy",
        "watches": [
            {
                "poll_interval_seconds": 30,
                "root": str(root),
                "settle_interval_seconds": 120,
                "watch_id": f"watch-{index}",
                "work_type": "anime",
            }
            for index, root in enumerate((watch_a, watch_b), 1)
        ],
    }

    restored = ConfigRevision.from_json(json.dumps(payload))

    assert [watch.library_root for watch in restored.watches] == [
        library.resolve(),
        library.resolve(),
    ]
    assert '"schema_version":5' in restored.to_json()
    assert restored.acgrip == AcgripConfig(enabled=False)
    assert (
        restored.subtitle_acquisition_policy
        is SubtitleAcquisitionPolicy.AUTOMATIC
    )
    assert restored.agent_budget.max_elapsed_seconds == 600
    assert "archive_routes" not in restored.public_payload()

    payload["schema_version"] = 2
    with pytest.raises(ServerError) as raised:
        ConfigRevision.from_json(json.dumps(payload))
    assert raised.value.code is ServerErrorCode.INVALID_CONFIG


def test_telegram_config_round_trips_without_public_destination(
    tmp_path: Path,
) -> None:
    draft = _draft(tmp_path)
    revision = ConfigRevision.create(
        revision_id="cfg-telegram",
        revision=3,
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        draft=ConfigDraft(
            watches=draft.watches,
            provider=draft.provider,
            apply_policy=draft.apply_policy,
            telegram=TelegramConfig(
                enabled=True,
                notification_types=(
                    NotificationType.PLAN_READY,
                    NotificationType.ATTENTION_REQUIRED,
                ),
                chat_id="-1001234567890",
                secret_ref="secret-telegram",
            ),
        ),
    )

    restored = ConfigRevision.from_json(revision.to_json())
    public = restored.public_payload()

    assert restored == revision
    assert public["telegram"] == {
        "enabled": True,
        "notification_types": ["plan_ready", "attention_required"],
        "destination_configured": True,
    }
    assert "-1001234567890" not in repr(public)
    assert "secret-telegram" not in repr(public)


def test_schema_v3_config_upgrades_with_telegram_disabled(
    tmp_path: Path,
) -> None:
    draft = _draft(tmp_path)
    revision = ConfigRevision.create(
        revision_id="cfg-v3",
        revision=2,
        created_at=datetime(2026, 8, 2, tzinfo=UTC),
        draft=draft,
    )
    payload = json.loads(revision.to_json())
    payload["schema_version"] = 3
    del payload["telegram"]
    del payload["acgrip"]
    del payload["subtitle_acquisition_policy"]

    restored = ConfigRevision.from_json(json.dumps(payload))

    assert not restored.telegram.enabled
    assert not restored.public_payload()["telegram"][
        "destination_configured"
    ]
    assert not restored.acgrip.enabled
    assert (
        restored.subtitle_acquisition_policy
        is SubtitleAcquisitionPolicy.AUTOMATIC
    )


def test_acgrip_opt_in_and_independent_policy_round_trip(
    tmp_path: Path,
) -> None:
    draft = _draft(tmp_path)
    revision = ConfigRevision.create(
        revision_id="cfg-acgrip",
        revision=4,
        created_at=datetime(2026, 8, 4, tzinfo=UTC),
        draft=ConfigDraft(
            watches=draft.watches,
            provider=draft.provider,
            apply_policy=ApplyPolicy.PLAN_ONLY,
            acgrip=AcgripConfig(enabled=True),
            subtitle_acquisition_policy=(
                SubtitleAcquisitionPolicy.MANUAL
            ),
        ),
    )

    restored = ConfigRevision.from_json(revision.to_json())

    assert restored.acgrip.enabled
    assert (
        restored.subtitle_acquisition_policy
        is SubtitleAcquisitionPolicy.MANUAL
    )
    assert restored.apply_policy is ApplyPolicy.PLAN_ONLY
    assert restored.public_payload()["acgrip"] == {"enabled": True}


def test_config_allows_explicit_shared_library_root(
    tmp_path: Path,
) -> None:
    draft = _draft(tmp_path)
    other = tmp_path / "other-watch"
    other.mkdir()

    result = ConfigDraft(
        watches=(
            draft.watches[0],
            WatchConfig(
                watch_id="watch-2",
                root=other,
                library_root=draft.watches[0].library_root,
                work_type=ServerWorkType.MOVIE,
                poll_interval_seconds=30,
                settle_interval_seconds=120,
            ),
        ),
        provider=draft.provider,
        apply_policy=draft.apply_policy,
    )

    assert result.watches[0].library_root == result.watches[1].library_root


def test_worker_resolves_library_root_by_exact_watch(
    tmp_path: Path,
) -> None:
    source_a = tmp_path / "source-a"
    source_b = tmp_path / "source-b"
    library_a = tmp_path / "library-a"
    library_b = tmp_path / "library-b"
    for path in (source_a, source_b, library_a, library_b):
        path.mkdir()
    draft = _draft(tmp_path)
    revision = ConfigRevision.create(
        revision_id="cfg-bound",
        revision=2,
        created_at=datetime(2026, 7, 25, tzinfo=UTC),
        draft=ConfigDraft(
            watches=(
                WatchConfig(
                    watch_id="watch-a",
                    root=source_a,
                    library_root=library_a,
                    work_type=ServerWorkType.ANIME,
                    poll_interval_seconds=30,
                    settle_interval_seconds=120,
                ),
                WatchConfig(
                    watch_id="watch-b",
                    root=source_b,
                    library_root=library_b,
                    work_type=ServerWorkType.ANIME,
                    poll_interval_seconds=30,
                    settle_interval_seconds=120,
                ),
            ),
            provider=draft.provider,
            apply_policy=draft.apply_policy,
        ),
    )
    job = AgentJobContext(
        registration=RunRegistration(
            run_id="run-b",
            job_id="job-b",
            discovery_id="discovery-b",
            config_revision=2,
            work_type=ServerWorkType.ANIME,
            source_capability="source-b",
        ),
        discovery=Discovery(
            discovery_id="discovery-b",
            watch_id="watch-b",
            config_revision=2,
            snapshot_id="snapshot-b",
            work_type=ServerWorkType.ANIME,
            discovered_at=datetime(2026, 7, 25, tzinfo=UTC),
        ),
    )

    watch, work_type = InitialAgentWorker._resolve_scope(job, revision)

    assert watch.watch_id == "watch-b"
    assert watch.library_root == library_b.resolve()
    assert work_type is TmdbWorkType.ANIME


@pytest.mark.parametrize(
    "url",
    [
        "http://models.example.test/v1",
        "https://user@models.example.test/v1",
        "https://models.example.test/v1?key=secret",
        "https://models.example.test/v1#fragment",
    ],
)
def test_provider_base_url_rejects_unsafe_urls(
    url: str,
) -> None:
    with pytest.raises(ServerError) as raised:
        validate_provider_base_url(url)

    assert raised.value.code is ServerErrorCode.PROVIDER_ORIGIN_REJECTED


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "https://models.example.test/v1",
        "https://models.example.test:8443/openai/v1",
    ],
)
def test_provider_base_url_allows_any_https_origin(url: str) -> None:
    assert validate_provider_base_url(url) == url


def test_model_provider_retries_transient_failures_five_times() -> None:
    lease = ControlledModelLease(
        config=ProviderConfig(
            base_url="https://127.0.0.1/v1",
            model="gpt-5",
            reasoning_effort="high",
            verbosity="low",
            secret_ref="secret-abc",
        ),
        api_key=b"explicit-secret",
    )

    assert lease._client.max_retries == 5
    asyncio.run(lease.close())


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
