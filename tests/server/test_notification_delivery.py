from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
    TelegramConfig,
)
from reeloom.server.notification_delivery import (
    ConfiguredNotificationDelivery,
    TelegramTestQueue,
)
from reeloom.server.notification_outbox import (
    ClaimedNotification,
    DeliveryResult,
    EnqueueResult,
)
from reeloom.server.notifications import (
    RenderedNotification,
    TelegramTestNotification,
)


def _config(revision: int) -> ConfigRevision:
    return ConfigRevision.create(
        revision_id=f"cfg-{revision}",
        revision=revision,
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        draft=ConfigDraft(
            watches=(),
            provider=ProviderConfig(
                base_url="https://provider.test/v1",
                model="gpt-5",
                secret_ref="secret-provider",
            ),
            apply_policy=ApplyPolicy.MANUAL,
            telegram=TelegramConfig(
                enabled=False,
                chat_id="-1001234567890",
                secret_ref=f"secret-telegram-{revision}",
            ),
        ),
    )


@dataclass
class _Configs:
    value: ConfigRevision | None

    def head(self) -> ConfigRevision | None:
        return self.value


@dataclass
class _Secrets:
    loaded: list[str] = field(default_factory=list)

    def load(self, reference: str) -> bytes:
        self.loaded.append(reference)
        return b"123456789:abcdefghijklmnopqrstuvwxyz_123456789"


@dataclass
class _Outbox:
    claims: list[ClaimedNotification]
    recovered: int = 0
    sent: list[tuple[str, int]] = field(default_factory=list)

    def recover_expired_leases(self, **_: object) -> int:
        self.recovered += 1
        return 2

    def claim(self, **_: object) -> ClaimedNotification | None:
        return self.claims.pop(0) if self.claims else None

    def settle_sent(self, **values: object) -> None:
        claim = values["claim"]
        assert isinstance(claim, ClaimedNotification)
        self.sent.append((claim.notification_id, values["message_id"]))

    def settle_failed(self, **_: object) -> None:
        raise AssertionError("unexpected failure")


@dataclass
class _Sender:
    token: str
    chat_id: str
    sent: list[RenderedNotification] = field(default_factory=list)
    closed: bool = False

    def send(self, notification: RenderedNotification) -> DeliveryResult:
        self.sent.append(notification)
        return DeliveryResult.sent(len(self.sent))

    def close(self) -> None:
        self.closed = True


def _claim(identifier: str, attempt: int = 1) -> ClaimedNotification:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    return ClaimedNotification(
        notification_id=identifier,
        dedupe_key=f"test:{identifier}",
        attempt_count=attempt,
        lease_owner="boot-1",
        lease_expires_at=now + timedelta(seconds=30),
        payload=TelegramTestNotification(),
    )


def test_delivery_recovers_rotates_and_drains_with_configured_credentials() -> None:
    configs = _Configs(_config(1))
    secrets = _Secrets()
    outbox = _Outbox([_claim("notification-1"), _claim("notification-2")])
    senders: list[_Sender] = []

    def factory(token: str, chat_id: str) -> _Sender:
        sender = _Sender(token, chat_id)
        senders.append(sender)
        return sender

    delivery = ConfiguredNotificationDelivery(
        configs=configs,
        secrets=secrets,
        outbox=outbox,  # type: ignore[arg-type]
        sender_factory=factory,
        worker_id="boot-1",
        clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert delivery.start() == 2
    assert delivery.run_once()
    configs.value = _config(2)
    assert delivery.run_once()
    delivery.close()

    assert outbox.recovered == 1
    assert outbox.sent == [("notification-1", 1), ("notification-2", 1)]
    assert secrets.loaded == ["secret-telegram-1", "secret-telegram-2"]
    assert len(senders) == 2
    assert all(sender.closed for sender in senders)
    assert all(sender.chat_id == "-1001234567890" for sender in senders)


def test_admin_test_queue_hashes_idempotency_key_and_emits_fixed_payload() -> None:
    values: list[dict[str, object]] = []

    class Outbox:
        def enqueue(self, **value: object) -> EnqueueResult:
            values.append(value)
            return EnqueueResult("notification-fixed", True)

    queue = TelegramTestQueue(
        configs=_Configs(_config(1)),
        outbox=Outbox(),  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
        id_factory=lambda: "generated",
    )

    result = queue.enqueue("browser-key with unsafe chars")

    assert result == {
        "notification_id": "notification-fixed",
        "state": "queued",
    }
    assert values[0]["notification_id"] == "notification-generated"
    assert str(values[0]["dedupe_key"]).startswith("test:")
    assert "browser-key" not in str(values[0]["dedupe_key"])
    assert isinstance(values[0]["payload"], TelegramTestNotification)
