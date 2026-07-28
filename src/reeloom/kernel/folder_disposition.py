from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.naming import filesystem_name_key
from reeloom.kernel.rename_plan import RootBinding, is_valid_plan_hash
from reeloom.kernel.schema import check_fields

CURRENT_FOLDER_DISPOSITION_SCHEMA = "folder-disposition-v1"
_MAX_BYTES = 64 * 1024
_INVENTORY_ID = re.compile(r"^folder-inventory-v1:[0-9a-f]{64}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_FIELDS = frozenset(
    {
        "action",
        "created_at",
        "file_count",
        "folder",
        "folder_generation_id",
        "inventory_id",
        "media_plan_hash",
        "reason_code",
        "run_id",
        "schema_version",
        "source_root",
        "target_relative",
    }
)
_FOLDER_FIELDS = frozenset({"device", "inode", "name"})
_ROOT_FIELDS = frozenset({"device", "inode", "path"})


class FolderDispositionAction(StrEnum):
    ARCHIVE = "archive"
    FAIL = "fail"
    REMOVE_EMPTY = "remove_empty"


def _invalid() -> DomainError:
    return DomainError(ErrorCode.INVALID_FIELD_TYPE)


def _duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _component(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or len(value.encode("utf-8", errors="surrogateescape")) > 255
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise _invalid()
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _invalid()
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _root_payload(root: RootBinding) -> dict[str, object]:
    return {
        "device": root.device,
        "inode": root.inode,
        "path": root.path.as_posix(),
    }


def _root(value: object) -> RootBinding:
    payload = check_fields(value, _ROOT_FIELDS, field="source_root")
    path = payload["path"]
    device = payload["device"]
    inode = payload["inode"]
    if (
        not isinstance(path, str)
        or not PurePosixPath(path).is_absolute()
        or type(device) is not int
        or device < 0
        or type(inode) is not int
        or inode < 0
    ):
        raise _invalid()
    return RootBinding(PurePosixPath(path), device, inode)


@dataclass(frozen=True, slots=True, init=False)
class FolderDispositionPlan:
    schema_version: str
    run_id: str
    folder_generation_id: str
    created_at: str
    source_root: RootBinding
    source_folder: str
    folder_device: int
    folder_inode: int
    inventory_id: str
    action: FolderDispositionAction
    target_relative: PurePosixPath | None
    media_plan_hash: str | None
    file_count: int
    reason_code: str
    plan_hash: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        folder_generation_id: str,
        created_at: datetime,
        source_root: RootBinding,
        source_folder: str,
        folder_device: int,
        folder_inode: int,
        inventory_id: str,
        action: FolderDispositionAction,
        target_relative: PurePosixPath | None,
        media_plan_hash: str | None,
        file_count: int,
        reason_code: str,
    ) -> FolderDispositionPlan:
        source_folder = _component(source_folder)
        if (
            not isinstance(run_id, str)
            or _OPAQUE.fullmatch(run_id) is None
            or not isinstance(folder_generation_id, str)
            or _OPAQUE.fullmatch(folder_generation_id) is None
            or not isinstance(source_root, RootBinding)
            or type(folder_device) is not int
            or folder_device < 0
            or type(folder_inode) is not int
            or folder_inode < 0
            or not isinstance(inventory_id, str)
            or _INVENTORY_ID.fullmatch(inventory_id) is None
            or not isinstance(action, FolderDispositionAction)
            or type(file_count) is not int
            or file_count < 0
            or not isinstance(reason_code, str)
            or _OPAQUE.fullmatch(reason_code) is None
            or (
                media_plan_hash is not None
                and not is_valid_plan_hash(media_plan_hash)
            )
        ):
            raise _invalid()
        if action is FolderDispositionAction.REMOVE_EMPTY:
            if target_relative is not None or file_count != 0:
                raise _invalid()
        else:
            if (
                not isinstance(target_relative, PurePosixPath)
                or target_relative.is_absolute()
                or len(target_relative.parts) != 2
                or target_relative.parts[0] != action.value
            ):
                raise _invalid()
            _component(target_relative.parts[1])
            if filesystem_name_key(target_relative.parts[1]).startswith("."):
                raise _invalid()
        plan = object.__new__(cls)
        object.__setattr__(
            plan, "schema_version", CURRENT_FOLDER_DISPOSITION_SCHEMA
        )
        object.__setattr__(plan, "run_id", run_id)
        object.__setattr__(
            plan, "folder_generation_id", folder_generation_id
        )
        object.__setattr__(plan, "created_at", _timestamp(created_at))
        object.__setattr__(plan, "source_root", source_root)
        object.__setattr__(plan, "source_folder", source_folder)
        object.__setattr__(plan, "folder_device", folder_device)
        object.__setattr__(plan, "folder_inode", folder_inode)
        object.__setattr__(plan, "inventory_id", inventory_id)
        object.__setattr__(plan, "action", action)
        object.__setattr__(plan, "target_relative", target_relative)
        object.__setattr__(plan, "media_plan_hash", media_plan_hash)
        object.__setattr__(plan, "file_count", file_count)
        object.__setattr__(plan, "reason_code", reason_code)
        object.__setattr__(plan, "plan_hash", "")
        object.__setattr__(
            plan,
            "plan_hash",
            "sha256:" + hashlib.sha256(plan.canonical_bytes()).hexdigest(),
        )
        return plan

    @classmethod
    def from_canonical_bytes(
        cls, content: bytes
    ) -> FolderDispositionPlan:
        if not isinstance(content, bytes) or not 0 < len(content) <= _MAX_BYTES:
            raise _invalid()
        try:
            raw = check_fields(
                json.loads(content, object_pairs_hook=_duplicate_pairs),
                _FIELDS,
                field="folder_disposition",
            )
            folder = check_fields(
                raw["folder"], _FOLDER_FIELDS, field="folder"
            )
            created_at = raw["created_at"]
            if not isinstance(created_at, str) or not created_at.endswith("Z"):
                raise _invalid()
            parsed = datetime.fromisoformat(created_at[:-1] + "+00:00")
            action = FolderDispositionAction(raw["action"])
            target = raw["target_relative"]
            plan = cls.create(
                run_id=raw["run_id"],  # type: ignore[arg-type]
                folder_generation_id=raw["folder_generation_id"],  # type: ignore[arg-type]
                created_at=parsed,
                source_root=_root(raw["source_root"]),
                source_folder=folder["name"],  # type: ignore[arg-type]
                folder_device=folder["device"],  # type: ignore[arg-type]
                folder_inode=folder["inode"],  # type: ignore[arg-type]
                inventory_id=raw["inventory_id"],  # type: ignore[arg-type]
                action=action,
                target_relative=(
                    None
                    if target is None
                    else PurePosixPath(target)  # type: ignore[arg-type]
                ),
                media_plan_hash=raw["media_plan_hash"],  # type: ignore[arg-type]
                file_count=raw["file_count"],  # type: ignore[arg-type]
                reason_code=raw["reason_code"],  # type: ignore[arg-type]
            )
        except (
            DomainError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            raise _invalid() from None
        if (
            raw["schema_version"] != CURRENT_FOLDER_DISPOSITION_SCHEMA
            or content != plan.canonical_bytes()
        ):
            raise _invalid()
        return plan

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "action": self.action.value,
                "created_at": self.created_at,
                "file_count": self.file_count,
                "folder": {
                    "device": self.folder_device,
                    "inode": self.folder_inode,
                    "name": self.source_folder,
                },
                "folder_generation_id": self.folder_generation_id,
                "inventory_id": self.inventory_id,
                "media_plan_hash": self.media_plan_hash,
                "reason_code": self.reason_code,
                "run_id": self.run_id,
                "schema_version": self.schema_version,
                "source_root": _root_payload(self.source_root),
                "target_relative": (
                    None
                    if self.target_relative is None
                    else self.target_relative.as_posix()
                ),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def verify_hash(self) -> bool:
        expected = "sha256:" + hashlib.sha256(
            self.canonical_bytes()
        ).hexdigest()
        return is_valid_plan_hash(self.plan_hash) and hmac.compare_digest(
            self.plan_hash, expected
        )


def verify_folder_disposition_bytes(
    content: bytes, plan_hash: str
) -> bool:
    try:
        plan = FolderDispositionPlan.from_canonical_bytes(content)
    except DomainError:
        return False
    return plan.verify_hash() and hmac.compare_digest(plan.plan_hash, plan_hash)
