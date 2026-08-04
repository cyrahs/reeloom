from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reeloom.adapters.telegram import validate_bot_token, validate_chat_id
from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    DEFAULT_AGENT_BUDGET,
    ProviderConfig,
    ServerWorkType,
    TelegramConfig,
    WatchConfig,
    agent_budget_from_payload,
)
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.notifications import NotificationType


@dataclass(frozen=True, slots=True)
class ConfigEdit:
    draft: ConfigDraft
    replacement_api_key: bytes | None
    replacement_telegram_token: bytes | None = None


def _root(
    value: object,
    *,
    retained: Path | None,
) -> Path:
    if isinstance(value, str):
        return Path(value)
    if not isinstance(value, dict):
        raise ServerError(ServerErrorCode.INVALID_CONFIG)
    if set(value) == {"mode"} and value["mode"] == "retain":
        if retained is None:
            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        return retained
    if (
        set(value) == {"mode", "path"}
        and value["mode"] == "replace"
        and isinstance(value["path"], str)
    ):
        return Path(value["path"])
    raise ServerError(ServerErrorCode.INVALID_CONFIG)


def parse_config_edit(
    value: dict[str, object],
    *,
    current: ConfigRevision | None,
) -> ConfigEdit:
    try:
        fields = {
            "apply_policy",
            "provider",
            "watches",
        }
        optional = {"agent_budget", "telegram"}
        keys = set(value)
        if not fields <= keys or not keys - fields <= optional:
            raise ValueError
        agent_budget = (
            agent_budget_from_payload(value["agent_budget"])
            if "agent_budget" in value
            else (
                current.agent_budget
                if current is not None
                else DEFAULT_AGENT_BUDGET
            )
        )
        raw_watches = value["watches"]
        raw_provider = value["provider"]
        if (
            not isinstance(raw_watches, list)
            or not isinstance(raw_provider, dict)
        ):
            raise ValueError
        common_provider_fields = {
            "base_url",
            "model",
            "reasoning_effort",
            "verbosity",
        }
        legacy_wire = (
            set(raw_provider) == common_provider_fields | {"api_key"}
        )
        edit_wire = (
            set(raw_provider) == common_provider_fields | {"credential"}
        )
        if not legacy_wire and not edit_wire:
            raise ValueError
        root_type = str if legacy_wire else dict
        old_watches = (
            {} if current is None else {
                item.watch_id: item for item in current.watches
            }
        )
        watches: list[WatchConfig] = []
        for item in raw_watches:
            if not isinstance(item, dict) or set(item) != {
                "library_root",
                "poll_interval_seconds",
                "root",
                "settle_interval_seconds",
                "watch_id",
                "work_type",
            }:
                raise ValueError
            if not isinstance(item["root"], root_type) or not isinstance(
                item["library_root"], root_type
            ):
                raise ValueError
            work_type = ServerWorkType(item["work_type"])
            watch_id = item["watch_id"]
            previous = (
                old_watches.get(watch_id)
                if isinstance(watch_id, str)
                else None
            )
            retained = (
                previous.root
                if previous is not None
                and previous.work_type is work_type
                else None
            )
            watches.append(
                WatchConfig(
                    watch_id=watch_id,
                    root=_root(item["root"], retained=retained),
                    library_root=_root(
                        item["library_root"],
                        retained=(
                            previous.library_root
                            if previous is not None
                            and previous.work_type is work_type
                            else None
                        ),
                    ),
                    work_type=work_type,
                    poll_interval_seconds=item["poll_interval_seconds"],
                    settle_interval_seconds=item[
                        "settle_interval_seconds"
                    ],
                )
            )
        replacement_api_key: bytes | None
        secret_ref: str
        if legacy_wire:
            api_key = raw_provider["api_key"]
            if not isinstance(api_key, str):
                raise ValueError
            replacement_api_key = api_key.encode("utf-8")
            secret_ref = "replacement-pending"
        else:
            credential = raw_provider["credential"]
            if not isinstance(credential, dict):
                raise ValueError
            if (
                set(credential) == {"mode"}
                and credential["mode"] == "retain"
            ):
                if current is None:
                    raise ValueError
                replacement_api_key = None
                secret_ref = current.provider.secret_ref
            elif (
                set(credential) == {"mode", "api_key"}
                and credential["mode"] == "replace"
                and isinstance(credential["api_key"], str)
            ):
                replacement_api_key = credential["api_key"].encode(
                    "utf-8"
                )
                secret_ref = "replacement-pending"
            else:
                raise ValueError
        telegram, replacement_telegram_token = _telegram_edit(
            value.get("telegram"),
            current=None if current is None else current.telegram,
        )
        draft = ConfigDraft(
            watches=tuple(watches),
            provider=ProviderConfig(
                base_url=raw_provider["base_url"],
                model=raw_provider["model"],
                reasoning_effort=raw_provider["reasoning_effort"],
                verbosity=raw_provider["verbosity"],
                secret_ref=secret_ref,
            ),
            apply_policy=ApplyPolicy(value["apply_policy"]),
            agent_budget=agent_budget,
            telegram=telegram,
        )
        if (
            replacement_api_key is not None
            and not 0 < len(replacement_api_key) <= 4_096
        ):
            raise ValueError
        return ConfigEdit(
            draft=draft,
            replacement_api_key=replacement_api_key,
            replacement_telegram_token=replacement_telegram_token,
        )
    except ServerError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ServerError(ServerErrorCode.INVALID_CONFIG) from None


def _telegram_edit(
    value: object,
    *,
    current: TelegramConfig | None,
) -> tuple[TelegramConfig, bytes | None]:
    if value is None:
        return current or TelegramConfig(), None
    if not isinstance(value, dict) or set(value) != {
        "destination",
        "enabled",
        "notification_types",
    }:
        raise ValueError
    raw_types = value["notification_types"]
    destination = value["destination"]
    if not isinstance(raw_types, list) or not isinstance(destination, dict):
        raise ValueError
    notification_types = tuple(NotificationType(item) for item in raw_types)
    if set(destination) == {"mode"} and destination["mode"] == "retain":
        if current is None or not current.secret_ref:
            raise ValueError
        chat_id = current.chat_id
        secret_ref = current.secret_ref
        replacement = None
    elif (
        set(destination) == {"mode"}
        and destination["mode"] == "unset"
        and value["enabled"] is False
    ):
        chat_id = ""
        secret_ref = ""
        replacement = None
    elif (
        set(destination) == {"mode", "bot_token", "chat_id"}
        and destination["mode"] == "replace"
    ):
        token = validate_bot_token(destination["bot_token"])
        chat_id = validate_chat_id(destination["chat_id"])
        secret_ref = "replacement-pending"
        replacement = token.encode("utf-8")
    else:
        raise ValueError
    return (
        TelegramConfig(
            enabled=value["enabled"],
            notification_types=notification_types,
            chat_id=chat_id,
            secret_ref=secret_ref,
        ),
        replacement,
    )
