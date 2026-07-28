from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.schema import check_fields

CURRENT_APPROVAL_SCHEMA_VERSION = "1"

_APPROVAL_FIELDS = frozenset(
    {
        "approval_id",
        "expires_at",
        "nonce",
        "plan_hash",
        "run_id",
        "schema_version",
        "scope",
    }
)
_APPROVAL_ID_PATTERN = re.compile(r"^approval-v1-[0-9a-f]{64}$")
_PLAN_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_MAX_CANONICAL_BYTES = 4096
_MAX_RUN_ID_BYTES = 128


class ApprovalScope(StrEnum):
    APPLY = "apply"
    FOLDER_DISPOSITION = "folder_disposition"


def _invalid_approval() -> DomainError:
    return DomainError(ErrorCode.INVALID_APPROVAL)


def _canonical_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _invalid_approval()
    try:
        if value.utcoffset() is None:
            raise _invalid_approval()
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except DomainError:
        raise
    except Exception:
        raise _invalid_approval() from None


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise _invalid_approval()
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise _invalid_approval() from None
    if _canonical_timestamp(parsed) != value:
        raise _invalid_approval()
    return parsed


def _binding_payload(
    *,
    run_id: str,
    plan_hash: str,
    scope: ApprovalScope,
    expires_at: str,
    nonce: str,
) -> dict[str, str]:
    return {
        "expires_at": expires_at,
        "nonce": nonce,
        "plan_hash": plan_hash,
        "run_id": run_id,
        "schema_version": CURRENT_APPROVAL_SCHEMA_VERSION,
        "scope": scope.value,
    }


def _canonical_json(payload: dict[str, str]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _approval_id(payload: dict[str, str]) -> str:
    digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return f"approval-v1-{digest}"


@dataclass(frozen=True, slots=True, init=False)
class ApprovalRecord:
    """An exact, immutable authorization for one plan and one apply scope."""

    schema_version: str
    approval_id: str
    run_id: str
    plan_hash: str
    scope: ApprovalScope
    expires_at: str
    nonce: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        plan_hash: str,
        scope: ApprovalScope,
        expires_at: datetime,
        nonce: str,
    ) -> ApprovalRecord:
        if (
            not isinstance(run_id, str)
            or not run_id
            or len(run_id.encode("utf-8")) > _MAX_RUN_ID_BYTES
            or any(
                unicodedata.category(character).startswith("C")
                for character in run_id
            )
            or not isinstance(plan_hash, str)
            or _PLAN_HASH_PATTERN.fullmatch(plan_hash) is None
            or not isinstance(scope, ApprovalScope)
            or not isinstance(nonce, str)
            or _NONCE_PATTERN.fullmatch(nonce) is None
        ):
            raise _invalid_approval()
        canonical_expiry = _canonical_timestamp(expires_at)
        binding = _binding_payload(
            run_id=run_id,
            plan_hash=plan_hash,
            scope=scope,
            expires_at=canonical_expiry,
            nonce=nonce,
        )
        record = object.__new__(cls)
        object.__setattr__(
            record,
            "schema_version",
            CURRENT_APPROVAL_SCHEMA_VERSION,
        )
        object.__setattr__(
            record,
            "approval_id",
            _approval_id(binding),
        )
        object.__setattr__(record, "run_id", run_id)
        object.__setattr__(record, "plan_hash", plan_hash)
        object.__setattr__(record, "scope", scope)
        object.__setattr__(record, "expires_at", canonical_expiry)
        object.__setattr__(record, "nonce", nonce)
        return record

    @classmethod
    def from_canonical_bytes(
        cls,
        canonical_bytes: bytes,
    ) -> ApprovalRecord:
        if (
            not isinstance(canonical_bytes, bytes)
            or not canonical_bytes
            or len(canonical_bytes) > _MAX_CANONICAL_BYTES
        ):
            raise _invalid_approval()
        try:
            payload = check_fields(
                json.loads(canonical_bytes.decode("utf-8")),
                _APPROVAL_FIELDS,
                field="approval",
            )
            raw_scope = payload["scope"]
            if not isinstance(raw_scope, str):
                raise _invalid_approval()
            record = cls.create(
                run_id=payload["run_id"],  # type: ignore[arg-type]
                plan_hash=payload["plan_hash"],  # type: ignore[arg-type]
                scope=ApprovalScope(raw_scope),
                expires_at=_parse_timestamp(payload["expires_at"]),
                nonce=payload["nonce"],  # type: ignore[arg-type]
            )
        except (DomainError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise _invalid_approval() from None
        if (
            payload["schema_version"] != CURRENT_APPROVAL_SCHEMA_VERSION
            or payload["approval_id"] != record.approval_id
            or canonical_bytes != record.canonical_bytes()
        ):
            raise _invalid_approval()
        return record

    @staticmethod
    def is_valid_id(value: object) -> bool:
        return (
            isinstance(value, str)
            and _APPROVAL_ID_PATTERN.fullmatch(value) is not None
        )

    def canonical_bytes(self) -> bytes:
        payload = _binding_payload(
            run_id=self.run_id,
            plan_hash=self.plan_hash,
            scope=self.scope,
            expires_at=self.expires_at,
            nonce=self.nonce,
        )
        payload["approval_id"] = self.approval_id
        return _canonical_json(payload)

    def verify_id(self) -> bool:
        expected = _approval_id(
            _binding_payload(
                run_id=self.run_id,
                plan_hash=self.plan_hash,
                scope=self.scope,
                expires_at=self.expires_at,
                nonce=self.nonce,
            )
        )
        return hmac.compare_digest(expected, self.approval_id)

    def is_expired(self, now: datetime) -> bool:
        canonical_now = _canonical_timestamp(now)
        return _parse_timestamp(canonical_now) >= _parse_timestamp(
            self.expires_at
        )
