from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.errors import ServerError, ServerErrorCode

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REASONING = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_VERBOSITY = frozenset({"low", "medium", "high"})


class ServerWorkType(StrEnum):
    ANIME = "anime"
    TV = "tv"
    MOVIE = "movie"


class ApplyPolicy(StrEnum):
    PLAN_ONLY = "plan_only"
    MANUAL = "manual"
    AUTOMATIC = "automatic"


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
    work_type: ServerWorkType
    poll_interval_seconds: int
    settle_interval_seconds: int

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
        ):
            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        object.__setattr__(self, "root", _root(self.root))


@dataclass(frozen=True, slots=True)
class ArchiveRoute:
    work_type: ServerWorkType
    root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.work_type, ServerWorkType):
            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        object.__setattr__(self, "root", _root(self.root))


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
class ConfigDraft:
    watches: tuple[WatchConfig, ...]
    archive_routes: tuple[ArchiveRoute, ...]
    provider: ProviderConfig
    apply_policy: ApplyPolicy

    def __post_init__(self) -> None:
        if (
            not isinstance(self.watches, tuple)
            or len(self.watches) > 1_000
            or not all(isinstance(item, WatchConfig) for item in self.watches)
            or not isinstance(self.archive_routes, tuple)
            or len(self.archive_routes) > len(ServerWorkType)
            or not all(
                isinstance(item, ArchiveRoute)
                for item in self.archive_routes
            )
            or not isinstance(self.provider, ProviderConfig)
            or not isinstance(self.apply_policy, ApplyPolicy)
        ):
            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        watch_ids = tuple(item.watch_id for item in self.watches)
        watch_roots = tuple(item.root for item in self.watches)
        route_types = tuple(item.work_type for item in self.archive_routes)
        route_roots = tuple(item.root for item in self.archive_routes)
        if (
            len(set(watch_ids)) != len(watch_ids)
            or len(set(watch_roots)) != len(watch_roots)
            or len(set(route_types)) != len(route_types)
            or len(set(route_roots)) != len(route_roots)
            or set(watch_roots) & set(route_roots)
            or any(item.work_type not in set(route_types) for item in self.watches)
        ):
            raise ServerError(ServerErrorCode.INVALID_CONFIG)


@dataclass(frozen=True, slots=True)
class ConfigDraftInput:
    watches: tuple[WatchConfig, ...]
    archive_routes: tuple[ArchiveRoute, ...]
    provider: ProviderConfigInput
    apply_policy: ApplyPolicy


@dataclass(frozen=True, slots=True)
class ConfigRevision:
    revision_id: str
    revision: int
    created_at: datetime
    watches: tuple[WatchConfig, ...]
    archive_routes: tuple[ArchiveRoute, ...]
    provider: ProviderConfig
    apply_policy: ApplyPolicy

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
            archive_routes=self.archive_routes,
            provider=self.provider,
            apply_policy=self.apply_policy,
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
            archive_routes=draft.archive_routes,
            provider=draft.provider,
            apply_policy=draft.apply_policy,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "apply_policy": self.apply_policy.value,
                "archive_routes": [
                    {
                        "root": str(item.root),
                        "work_type": item.work_type.value,
                    }
                    for item in self.archive_routes
                ],
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
                "watches": [
                    {
                        "poll_interval_seconds": item.poll_interval_seconds,
                        "root": str(item.root),
                        "settle_interval_seconds": item.settle_interval_seconds,
                        "watch_id": item.watch_id,
                        "work_type": item.work_type.value,
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
                }
                for item in self.watches
            ],
            "archive_routes": [
                {"work_type": item.work_type.value}
                for item in self.archive_routes
            ],
            "provider": {
                "base_url": self.provider.base_url,
                "model": self.provider.model,
                "reasoning_effort": self.provider.reasoning_effort,
                "verbosity": self.provider.verbosity,
            },
            "apply_policy": self.apply_policy.value,
        }

    @classmethod
    def from_json(cls, value: str) -> ConfigRevision:
        try:
            raw: Any = json.loads(value)
            if not isinstance(raw, dict) or set(raw) != {
                "apply_policy",
                "archive_routes",
                "created_at",
                "provider",
                "revision",
                "revision_id",
                "watches",
            }:
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
            watches = tuple(
                WatchConfig(
                    watch_id=item["watch_id"],
                    root=Path(item["root"]),
                    work_type=ServerWorkType(item["work_type"]),
                    poll_interval_seconds=item["poll_interval_seconds"],
                    settle_interval_seconds=item[
                        "settle_interval_seconds"
                    ],
                )
                for item in raw["watches"]
            )
            routes = tuple(
                ArchiveRoute(
                    work_type=ServerWorkType(item["work_type"]),
                    root=Path(item["root"]),
                )
                for item in raw["archive_routes"]
            )
            return cls(
                revision_id=raw["revision_id"],
                revision=raw["revision"],
                created_at=datetime.fromisoformat(raw["created_at"]),
                watches=watches,
                archive_routes=routes,
                provider=ProviderConfig(
                    base_url=provider["base_url"],
                    model=provider["model"],
                    reasoning_effort=provider["reasoning_effort"],
                    verbosity=provider["verbosity"],
                    secret_ref=provider["secret_ref"],
                ),
                apply_policy=ApplyPolicy(raw["apply_policy"]),
            )
        except (KeyError, TypeError, ValueError, ServerError):
            raise ServerError(ServerErrorCode.INVALID_CONFIG) from None
