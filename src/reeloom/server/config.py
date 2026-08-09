from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.errors import RuntimeDomainError
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.notifications import NotificationType

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TELEGRAM_CHAT_ID = re.compile(r"^-?[0-9]{1,20}$")
_REASONING = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_VERBOSITY = frozenset({"low", "medium", "high"})
MAX_MODEL_TURNS = 1_024
MAX_TOOL_CALLS = 4_096
MAX_FAILURES = 100
MAX_TOTAL_TOKENS = 10_000_000
DEFAULT_AGENT_BUDGET = RunBudget()
TELEGRAM_EVENT_TYPES = (
    NotificationType.PLAN_READY,
    NotificationType.ARCHIVE_COMPLETED,
    NotificationType.ATTENTION_REQUIRED,
)


class ServerWorkType(StrEnum):
    ANIME = "anime"
    TV = "tv"
    MOVIE = "movie"


class ApplyPolicy(StrEnum):
    PLAN_ONLY = "plan_only"
    MANUAL = "manual"
    AUTOMATIC = "automatic"


class SubtitleAcquisitionPolicy(StrEnum):
    PLAN_ONLY = "plan_only"
    MANUAL = "manual"
    AUTOMATIC = "automatic"


class SubtitleProvider(StrEnum):
    ACGRIP = "acgrip"


SUBTITLE_PROVIDERS_BY_WORK_TYPE = {
    ServerWorkType.ANIME: frozenset({SubtitleProvider.ACGRIP}),
    ServerWorkType.TV: frozenset(),
    ServerWorkType.MOVIE: frozenset(),
}


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionConfig:
    enabled: bool = False
    provider: SubtitleProvider | None = None
    policy: SubtitleAcquisitionPolicy = SubtitleAcquisitionPolicy.AUTOMATIC

    def __post_init__(self) -> None:
        if (
            type(self.enabled) is not bool
            or (
                self.provider is not None
                and not isinstance(self.provider, SubtitleProvider)
            )
            or not isinstance(self.policy, SubtitleAcquisitionPolicy)
            or (self.enabled and self.provider is None)
        ):
            raise ServerError(ServerErrorCode.INVALID_CONFIG)


DEFAULT_SUBTITLE_ACQUISITION_CONFIG = SubtitleAcquisitionConfig()


def _opaque(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_ID.fullmatch(value) is not None


def _root(value: object) -> Path:
    if not isinstance(value, Path):
        raise ServerError(ServerErrorCode.INVALID_CONFIG)
    try:
        return AuthorizedRoot.create(value).path
    except Exception:
        raise ServerError(ServerErrorCode.INVALID_CONFIG) from None


@dataclass(frozen=True, slots=True)
class WatchConfig:
    watch_id: str
    root: Path
    library_root: Path
    work_type: ServerWorkType
    poll_interval_seconds: int
    settle_interval_seconds: int
    subtitle_acquisition: SubtitleAcquisitionConfig = (
        DEFAULT_SUBTITLE_ACQUISITION_CONFIG
    )

    def __post_init__(self) -> None:
        if (
            not _opaque(self.watch_id)
            or not isinstance(self.work_type, ServerWorkType)
            or type(self.poll_interval_seconds) is not int
            or not 1 <= self.poll_interval_seconds <= 86_400
            or type(self.settle_interval_seconds) is not int
            or not self.poll_interval_seconds
            <= self.settle_interval_seconds
            <= 604_800
            or not isinstance(
                self.subtitle_acquisition, SubtitleAcquisitionConfig
            )
            or (
                self.subtitle_acquisition.provider is not None
                and self.subtitle_acquisition.provider
                not in SUBTITLE_PROVIDERS_BY_WORK_TYPE[self.work_type]
            )
            or (
                self.subtitle_acquisition.enabled
                and not SUBTITLE_PROVIDERS_BY_WORK_TYPE[self.work_type]
            )
        ):
            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        object.__setattr__(self, "root", _root(self.root))
        object.__setattr__(self, "library_root", _root(self.library_root))


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    base_url: str
    model: str
    reasoning_effort: str | None = None
    verbosity: str | None = None
    secret_ref: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.base_url, str)
            or not self.base_url
            or len(self.base_url.encode("utf-8")) > 2_048
            or not isinstance(self.model, str)
            or _MODEL.fullmatch(self.model) is None
            or (
                self.reasoning_effort is not None
                and self.reasoning_effort not in _REASONING
            )
            or (
                self.verbosity is not None
                and self.verbosity not in _VERBOSITY
            )
            or not _opaque(self.secret_ref)
        ):
            raise ServerError(ServerErrorCode.INVALID_CONFIG)


@dataclass(frozen=True, slots=True)
class ProviderConfigInput:
    base_url: str
    model: str
    api_key: bytes
    reasoning_effort: str | None = None
    verbosity: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.api_key, bytes)
            or not 0 < len(self.api_key) <= 4_096
        ):
            raise ServerError(ServerErrorCode.INVALID_SECRET)


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    enabled: bool = False
    notification_types: tuple[NotificationType, ...] = TELEGRAM_EVENT_TYPES
    chat_id: str = ""
    secret_ref: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.enabled) is not bool
            or not isinstance(self.notification_types, tuple)
            or not self.notification_types
            or len(set(self.notification_types)) != len(self.notification_types)
            or not all(
                item in TELEGRAM_EVENT_TYPES
                for item in self.notification_types
            )
            or (bool(self.chat_id) != bool(self.secret_ref))
            or (
                self.chat_id
                and _TELEGRAM_CHAT_ID.fullmatch(self.chat_id) is None
            )
            or (self.secret_ref and not _opaque(self.secret_ref))
            or self.enabled
            and not self.secret_ref
        ):
            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        object.__setattr__(
            self,
            "notification_types",
            tuple(
                item
                for item in TELEGRAM_EVENT_TYPES
                if item in self.notification_types
            ),
        )


DEFAULT_TELEGRAM_CONFIG = TelegramConfig()


@dataclass(frozen=True, slots=True)
class _LegacyAcgripConfig:
    enabled: bool = False

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ServerError(ServerErrorCode.INVALID_CONFIG)


@dataclass(frozen=True, slots=True)
class ConfigDraft:
    watches: tuple[WatchConfig, ...]
    provider: ProviderConfig
    apply_policy: ApplyPolicy
    agent_budget: RunBudget = DEFAULT_AGENT_BUDGET
    telegram: TelegramConfig = DEFAULT_TELEGRAM_CONFIG

    def __post_init__(self) -> None:
        if (
            not isinstance(self.watches, tuple)
            or len(self.watches) > 1_000
            or not all(isinstance(item, WatchConfig) for item in self.watches)
            or not isinstance(self.provider, ProviderConfig)
            or not isinstance(self.apply_policy, ApplyPolicy)
            or not isinstance(self.agent_budget, RunBudget)
            or not isinstance(self.telegram, TelegramConfig)
            or self.agent_budget.max_model_turns > MAX_MODEL_TURNS
            or self.agent_budget.max_tool_calls > MAX_TOOL_CALLS
            or self.agent_budget.max_failures > MAX_FAILURES
            or self.agent_budget.max_total_tokens > MAX_TOTAL_TOKENS
            or self.agent_budget.max_elapsed_seconds < 1
        ):
            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        watch_ids = tuple(item.watch_id for item in self.watches)
        watch_roots = tuple(item.root for item in self.watches)
        library_roots = tuple(item.library_root for item in self.watches)
        if (
            len(set(watch_ids)) != len(watch_ids)
            or len(set(watch_roots)) != len(watch_roots)
            or set(watch_roots) & set(library_roots)
        ):
            raise ServerError(ServerErrorCode.INVALID_CONFIG)


@dataclass(frozen=True, slots=True)
class ConfigDraftInput:
    watches: tuple[WatchConfig, ...]
    provider: ProviderConfigInput
    apply_policy: ApplyPolicy
    agent_budget: RunBudget = DEFAULT_AGENT_BUDGET
    telegram: TelegramConfig = DEFAULT_TELEGRAM_CONFIG


@dataclass(frozen=True, slots=True)
class ConfigRevision:
    revision_id: str
    revision: int
    created_at: datetime
    watches: tuple[WatchConfig, ...]
    provider: ProviderConfig
    apply_policy: ApplyPolicy
    agent_budget: RunBudget = DEFAULT_AGENT_BUDGET
    telegram: TelegramConfig = DEFAULT_TELEGRAM_CONFIG

    def __post_init__(self) -> None:
        if (
            not _opaque(self.revision_id)
            or type(self.revision) is not int
            or self.revision < 1
            or not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
        ):
            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        ConfigDraft(
            watches=self.watches,
            provider=self.provider,
            apply_policy=self.apply_policy,
            agent_budget=self.agent_budget,
            telegram=self.telegram,
        )

    @classmethod
    def create(
        cls,
        *,
        revision_id: str,
        revision: int,
        created_at: datetime,
        draft: ConfigDraft,
    ) -> ConfigRevision:
        return cls(
            revision_id=revision_id,
            revision=revision,
            created_at=created_at,
            watches=draft.watches,
            provider=draft.provider,
            apply_policy=draft.apply_policy,
            agent_budget=draft.agent_budget,
            telegram=draft.telegram,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "apply_policy": self.apply_policy.value,
                "created_at": self.created_at.isoformat(),
                "provider": {
                    "base_url": self.provider.base_url,
                    "model": self.provider.model,
                    "reasoning_effort": self.provider.reasoning_effort,
                    "secret_ref": self.provider.secret_ref,
                    "verbosity": self.provider.verbosity,
                },
                "revision": self.revision,
                "revision_id": self.revision_id,
                "schema_version": 6,
                "agent_budget": _budget_payload(self.agent_budget),
                "telegram": {
                    "chat_id": self.telegram.chat_id,
                    "enabled": self.telegram.enabled,
                    "notification_types": [
                        item.value
                        for item in self.telegram.notification_types
                    ],
                    "secret_ref": self.telegram.secret_ref,
                },
                "watches": [
                    {
                        "library_root": str(item.library_root),
                        "poll_interval_seconds": item.poll_interval_seconds,
                        "root": str(item.root),
                        "settle_interval_seconds": item.settle_interval_seconds,
                        "watch_id": item.watch_id,
                        "work_type": item.work_type.value,
                        "subtitle_acquisition": (
                            _subtitle_acquisition_payload(
                                item.subtitle_acquisition
                            )
                        ),
                    }
                    for item in self.watches
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def public_payload(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "revision_id": self.revision_id,
            "watches": [
                {
                    "watch_id": item.watch_id,
                    "work_type": item.work_type.value,
                    "poll_interval_seconds": item.poll_interval_seconds,
                    "settle_interval_seconds": item.settle_interval_seconds,
                    "root": str(item.root),
                    "library_root": str(item.library_root),
                    "subtitle_acquisition": (
                        _subtitle_acquisition_payload(
                            item.subtitle_acquisition
                        )
                    ),
                }
                for item in self.watches
            ],
            "provider": {
                "base_url": self.provider.base_url,
                "model": self.provider.model,
                "reasoning_effort": self.provider.reasoning_effort,
                "verbosity": self.provider.verbosity,
                "api_key_configured": True,
            },
            "apply_policy": self.apply_policy.value,
            "agent_budget": _budget_payload(self.agent_budget),
            "telegram": {
                "enabled": self.telegram.enabled,
                "notification_types": [
                    item.value for item in self.telegram.notification_types
                ],
                "destination_configured": bool(
                    self.telegram.chat_id and self.telegram.secret_ref
                ),
            },
        }

    @classmethod
    def from_json(cls, value: str) -> ConfigRevision:
        try:
            raw: Any = json.loads(value)
            if not isinstance(raw, dict):
                raise ValueError
            provider = raw["provider"]
            if not isinstance(provider, dict) or set(provider) != {
                "base_url",
                "model",
                "reasoning_effort",
                "secret_ref",
                "verbosity",
            }:
                raise ValueError
            common = {
                "apply_policy",
                "created_at",
                "provider",
                "revision",
                "revision_id",
                "watches",
            }
            budget = DEFAULT_AGENT_BUDGET
            telegram = DEFAULT_TELEGRAM_CONFIG
            if set(raw) == common | {"archive_routes"}:
                watches = _legacy_watches(
                    raw["watches"],
                    raw["archive_routes"],
                )
            elif (
                set(raw) == common | {"schema_version"}
                and type(raw["schema_version"]) is int
                and raw["schema_version"] == 2
            ):
                watches = _v2_watches(raw["watches"])
            elif (
                set(raw)
                == common | {"schema_version", "agent_budget"}
                and type(raw["schema_version"]) is int
                and raw["schema_version"] == 3
            ):
                watches = _v2_watches(raw["watches"])
                budget = agent_budget_from_payload(raw["agent_budget"])
            elif (
                set(raw)
                == common
                | {"schema_version", "agent_budget", "telegram"}
                and type(raw["schema_version"]) is int
                and raw["schema_version"] == 4
            ):
                watches = _v2_watches(raw["watches"])
                budget = agent_budget_from_payload(raw["agent_budget"])
                telegram = telegram_config_from_payload(raw["telegram"])
            elif (
                set(raw)
                == common
                | {
                    "schema_version",
                    "agent_budget",
                    "telegram",
                    "acgrip",
                    "subtitle_acquisition_policy",
                }
                and type(raw["schema_version"]) is int
                and raw["schema_version"] == 5
            ):
                watches = _v2_watches(raw["watches"])
                budget = agent_budget_from_payload(raw["agent_budget"])
                telegram = telegram_config_from_payload(raw["telegram"])
                acgrip = _legacy_acgrip_from_payload(raw["acgrip"])
                policy = SubtitleAcquisitionPolicy(
                    raw["subtitle_acquisition_policy"]
                )
                watches = _migrate_v5_subtitle_acquisition(
                    watches,
                    acgrip=acgrip,
                    policy=policy,
                )
            elif (
                set(raw)
                == common
                | {"schema_version", "agent_budget", "telegram"}
                and type(raw["schema_version"]) is int
                and raw["schema_version"] == 6
            ):
                watches = _v6_watches(raw["watches"])
                budget = agent_budget_from_payload(raw["agent_budget"])
                telegram = telegram_config_from_payload(raw["telegram"])
            else:
                raise ValueError
            return cls(
                revision_id=raw["revision_id"],
                revision=raw["revision"],
                created_at=datetime.fromisoformat(raw["created_at"]),
                watches=watches,
                provider=ProviderConfig(
                    base_url=provider["base_url"],
                    model=provider["model"],
                    reasoning_effort=provider["reasoning_effort"],
                    verbosity=provider["verbosity"],
                    secret_ref=provider["secret_ref"],
                ),
                apply_policy=ApplyPolicy(raw["apply_policy"]),
                agent_budget=budget,
                telegram=telegram,
            )
        except (KeyError, TypeError, ValueError, ServerError):
            raise ServerError(ServerErrorCode.INVALID_CONFIG) from None


def _budget_payload(budget: RunBudget) -> dict[str, int | float]:
    return {
        "max_model_turns": budget.max_model_turns,
        "max_tool_calls": budget.max_tool_calls,
        "max_failures": budget.max_failures,
        "max_total_tokens": budget.max_total_tokens,
        "max_elapsed_seconds": budget.max_elapsed_seconds,
    }


def agent_budget_from_payload(value: object) -> RunBudget:
    fields = {
        "max_model_turns",
        "max_tool_calls",
        "max_failures",
        "max_total_tokens",
        "max_elapsed_seconds",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError
    try:
        budget = RunBudget(
            max_model_turns=value["max_model_turns"],
            max_tool_calls=value["max_tool_calls"],
            max_failures=value["max_failures"],
            max_total_tokens=value["max_total_tokens"],
            max_elapsed_seconds=value["max_elapsed_seconds"],
        )
    except RuntimeDomainError:
        raise ValueError from None
    if (
        budget.max_model_turns > MAX_MODEL_TURNS
        or budget.max_tool_calls > MAX_TOOL_CALLS
        or budget.max_failures > MAX_FAILURES
        or budget.max_total_tokens > MAX_TOTAL_TOKENS
    ):
        raise ValueError
    return budget


def telegram_config_from_payload(value: object) -> TelegramConfig:
    fields = {"chat_id", "enabled", "notification_types", "secret_ref"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError
    notification_types = value["notification_types"]
    if not isinstance(notification_types, list):
        raise ValueError
    try:
        return TelegramConfig(
            enabled=value["enabled"],
            notification_types=tuple(
                NotificationType(item) for item in notification_types
            ),
            chat_id=value["chat_id"],
            secret_ref=value["secret_ref"],
        )
    except (TypeError, ValueError, ServerError):
        raise ValueError from None


def _legacy_acgrip_from_payload(value: object) -> _LegacyAcgripConfig:
    if not isinstance(value, dict) or set(value) != {"enabled"}:
        raise ValueError
    try:
        return _LegacyAcgripConfig(enabled=value["enabled"])
    except ServerError:
        raise ValueError from None


def _subtitle_acquisition_payload(
    value: SubtitleAcquisitionConfig,
) -> dict[str, object]:
    return {
        "enabled": value.enabled,
        "provider": None if value.provider is None else value.provider.value,
        "policy": value.policy.value,
    }


def subtitle_acquisition_from_payload(
    value: object,
) -> SubtitleAcquisitionConfig:
    if not isinstance(value, dict) or set(value) != {
        "enabled",
        "provider",
        "policy",
    }:
        raise ValueError
    provider = value["provider"]
    try:
        return SubtitleAcquisitionConfig(
            enabled=value["enabled"],
            provider=(
                None if provider is None else SubtitleProvider(provider)
            ),
            policy=SubtitleAcquisitionPolicy(value["policy"]),
        )
    except (TypeError, ValueError, ServerError):
        raise ValueError from None


def _v2_watches(value: object) -> tuple[WatchConfig, ...]:
    if not isinstance(value, list):
        raise ValueError
    fields = {
        "library_root",
        "poll_interval_seconds",
        "root",
        "settle_interval_seconds",
        "watch_id",
        "work_type",
    }
    if any(not isinstance(item, dict) or set(item) != fields for item in value):
        raise ValueError
    return tuple(
        WatchConfig(
            watch_id=item["watch_id"],
            root=Path(item["root"]),
            library_root=Path(item["library_root"]),
            work_type=ServerWorkType(item["work_type"]),
            poll_interval_seconds=item["poll_interval_seconds"],
            settle_interval_seconds=item["settle_interval_seconds"],
        )
        for item in value
    )


def _v6_watches(value: object) -> tuple[WatchConfig, ...]:
    if not isinstance(value, list):
        raise ValueError
    fields = {
        "library_root",
        "poll_interval_seconds",
        "root",
        "settle_interval_seconds",
        "subtitle_acquisition",
        "watch_id",
        "work_type",
    }
    if any(not isinstance(item, dict) or set(item) != fields for item in value):
        raise ValueError
    return tuple(
        WatchConfig(
            watch_id=item["watch_id"],
            root=Path(item["root"]),
            library_root=Path(item["library_root"]),
            work_type=ServerWorkType(item["work_type"]),
            poll_interval_seconds=item["poll_interval_seconds"],
            settle_interval_seconds=item["settle_interval_seconds"],
            subtitle_acquisition=subtitle_acquisition_from_payload(
                item["subtitle_acquisition"]
            ),
        )
        for item in value
    )


def _migrate_v5_subtitle_acquisition(
    watches: tuple[WatchConfig, ...],
    *,
    acgrip: _LegacyAcgripConfig,
    policy: SubtitleAcquisitionPolicy,
) -> tuple[WatchConfig, ...]:
    return tuple(
        WatchConfig(
            watch_id=item.watch_id,
            root=item.root,
            library_root=item.library_root,
            work_type=item.work_type,
            poll_interval_seconds=item.poll_interval_seconds,
            settle_interval_seconds=item.settle_interval_seconds,
            subtitle_acquisition=(
                SubtitleAcquisitionConfig(
                    enabled=acgrip.enabled,
                    provider=SubtitleProvider.ACGRIP,
                    policy=policy,
                )
                if item.work_type is ServerWorkType.ANIME
                else DEFAULT_SUBTITLE_ACQUISITION_CONFIG
            ),
        )
        for item in watches
    )


def _legacy_watches(
    watches_value: object,
    routes_value: object,
) -> tuple[WatchConfig, ...]:
    if not isinstance(watches_value, list) or not isinstance(
        routes_value, list
    ):
        raise ValueError
    watch_fields = {
        "poll_interval_seconds",
        "root",
        "settle_interval_seconds",
        "watch_id",
        "work_type",
    }
    route_fields = {"root", "work_type"}
    if any(
        not isinstance(item, dict) or set(item) != watch_fields
        for item in watches_value
    ) or any(
        not isinstance(item, dict) or set(item) != route_fields
        for item in routes_value
    ):
        raise ValueError
    routes = tuple(
        (
            ServerWorkType(item["work_type"]),
            _root(Path(item["root"])),
        )
        for item in routes_value
    )
    if (
        len({work_type for work_type, _ in routes}) != len(routes)
        or len({root for _, root in routes}) != len(routes)
    ):
        raise ValueError
    route_by_type = dict(routes)
    watches = tuple(
        WatchConfig(
            watch_id=item["watch_id"],
            root=Path(item["root"]),
            library_root=route_by_type[ServerWorkType(item["work_type"])],
            work_type=ServerWorkType(item["work_type"]),
            poll_interval_seconds=item["poll_interval_seconds"],
            settle_interval_seconds=item["settle_interval_seconds"],
        )
        for item in watches_value
    )
    if {item.root for item in watches} & {root for _, root in routes}:
        raise ValueError
    return watches
