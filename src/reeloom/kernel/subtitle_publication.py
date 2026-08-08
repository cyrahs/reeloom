from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.naming import filesystem_name_key
from reeloom.kernel.subtitle_acquisition import (
    MAX_ARCHIVE_ENTRIES,
    MAX_SUBTITLE_MEMBER_BYTES,
    MAX_TOTAL_SUBTITLE_BYTES,
    SubtitleAcquisitionPlan,
)

CURRENT_SUBTITLE_PUBLICATION_SCHEMA_VERSION = "1"
SUBTITLE_PUBLICATION_MARKER = ".reeloom-complete-v1.json"
MAX_SUBTITLE_PUBLICATION_MARKER_BYTES = 128 * 1024

_PLAN_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_PUBLICATION_DIRECTORY = re.compile(r"^reeloom-acquired-[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _invalid() -> DomainError:
    return DomainError(ErrorCode.INVALID_FIELD_TYPE)


def _member_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", "..", SUBTITLE_PUBLICATION_MARKER}
        or "/" in value
        or "\\" in value
        or value.casefold().startswith(".env")
        or len(value.encode("utf-8", errors="surrogateescape")) > 255
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise _invalid()
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _invalid()
    return value


@dataclass(frozen=True, slots=True, order=True)
class SubtitlePublicationMember:
    name: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _member_name(self.name))
        if (
            type(self.size_bytes) is not int
            or not 1 <= self.size_bytes <= MAX_SUBTITLE_MEMBER_BYTES
        ):
            raise _invalid()
        object.__setattr__(self, "sha256", _digest(self.sha256))

    @classmethod
    def from_payload(cls, value: object) -> SubtitlePublicationMember:
        if not isinstance(value, dict) or set(value) != {
            "name",
            "sha256",
            "size_bytes",
        }:
            raise _invalid()
        return cls(
            name=value["name"],  # type: ignore[arg-type]
            size_bytes=value["size_bytes"],  # type: ignore[arg-type]
            sha256=value["sha256"],  # type: ignore[arg-type]
        )

    def payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True, init=False)
class SubtitlePublicationManifest:
    schema_version: str
    plan_hash: str
    publication_directory: str
    members: tuple[SubtitlePublicationMember, ...]

    @classmethod
    def create(
        cls,
        *,
        plan_hash: str,
        publication_directory: str,
        members: tuple[SubtitlePublicationMember, ...],
    ) -> SubtitlePublicationManifest:
        if (
            not isinstance(plan_hash, str)
            or _PLAN_HASH.fullmatch(plan_hash) is None
            or not isinstance(publication_directory, str)
            or _PUBLICATION_DIRECTORY.fullmatch(publication_directory) is None
            or publication_directory
            != f"reeloom-acquired-{plan_hash.removeprefix('sha256:')}"
            or not isinstance(members, tuple)
            or not 1 <= len(members) <= MAX_ARCHIVE_ENTRIES
            or any(
                not isinstance(item, SubtitlePublicationMember)
                for item in members
            )
        ):
            raise _invalid()
        ordered = tuple(
            sorted(
                members,
                key=lambda item: (filesystem_name_key(item.name), item.name),
            )
        )
        if (
            len({filesystem_name_key(item.name) for item in ordered})
            != len(ordered)
            or sum(item.size_bytes for item in ordered)
            > MAX_TOTAL_SUBTITLE_BYTES
        ):
            raise _invalid()
        manifest = object.__new__(cls)
        object.__setattr__(
            manifest,
            "schema_version",
            CURRENT_SUBTITLE_PUBLICATION_SCHEMA_VERSION,
        )
        object.__setattr__(manifest, "plan_hash", plan_hash)
        object.__setattr__(
            manifest,
            "publication_directory",
            publication_directory,
        )
        object.__setattr__(manifest, "members", ordered)
        return manifest

    @classmethod
    def from_plan(
        cls,
        plan: SubtitleAcquisitionPlan,
    ) -> SubtitlePublicationManifest:
        if not isinstance(plan, SubtitleAcquisitionPlan) or not plan.verify_hash():
            raise _invalid()
        return cls.create(
            plan_hash=plan.plan_hash,
            publication_directory=plan.destination_directory.as_posix(),
            members=tuple(
                SubtitlePublicationMember(
                    name=member.destination_name,
                    size_bytes=member.size_bytes,
                    sha256=member.sha256,
                )
                for member in plan.members
            ),
        )

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "members": [item.payload() for item in self.members],
                "plan_hash": self.plan_hash,
                "publication_directory": self.publication_directory,
                "schema_version": self.schema_version,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_canonical_bytes(
        cls,
        content: bytes,
    ) -> SubtitlePublicationManifest:
        if (
            not isinstance(content, bytes)
            or not 0 < len(content) <= MAX_SUBTITLE_PUBLICATION_MARKER_BYTES
        ):
            raise _invalid()
        try:
            raw = json.loads(content)
            if not isinstance(raw, dict) or set(raw) != {
                "members",
                "plan_hash",
                "publication_directory",
                "schema_version",
            }:
                raise _invalid()
            if raw["schema_version"] != CURRENT_SUBTITLE_PUBLICATION_SCHEMA_VERSION:
                raise _invalid()
            raw_members = raw["members"]
            if not isinstance(raw_members, list):
                raise _invalid()
            manifest = cls.create(
                plan_hash=raw["plan_hash"],
                publication_directory=raw["publication_directory"],
                members=tuple(
                    SubtitlePublicationMember.from_payload(item)
                    for item in raw_members
                ),
            )
        except (
            DomainError,
            json.JSONDecodeError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
        ):
            raise _invalid() from None
        if not hmac.compare_digest(manifest.canonical_bytes(), content):
            raise _invalid()
        return manifest
