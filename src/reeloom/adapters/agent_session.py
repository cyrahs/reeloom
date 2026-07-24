from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import unicodedata
from enum import StrEnum

from agents.items import TResponseInputItem
from agents.memory import SessionSettings

from reeloom.adapters._immutable_file import (
    ImmutableFileError,
    ImmutableFileErrorCode,
    open_root,
    read_at,
    write_once_at,
)
from reeloom.policy.path_policy import AuthorizedRoot

_SCHEMA_VERSION = "agent-session-record-v1"
_FILE_NAME = re.compile(r"^session-([0-9]{8})\.json$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_RECORD_BYTES = 4 * 1024 * 1024
_MAX_SESSION_ID_BYTES = 128
_MAX_ITEMS = 10_000
_FIELDS = frozenset(
    {
        "items",
        "operation",
        "previous_digest",
        "record_digest",
        "schema_version",
        "sequence",
        "session_id",
    }
)
_BODY_FIELDS = _FIELDS - {"record_digest"}


class AgentSessionErrorCode(StrEnum):
    CORRUPT = "agent_session_corrupt"
    CONFLICT = "agent_session_conflict"
    FAILURE = "agent_session_failure"
    LIMIT_EXCEEDED = "agent_session_limit_exceeded"


class AgentSessionError(RuntimeError):
    def __init__(self, code: AgentSessionErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def _canonical(payload: dict[str, object]) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, UnicodeEncodeError, ValueError):
        raise AgentSessionError(AgentSessionErrorCode.FAILURE) from None


def _copy_items(
    items: list[TResponseInputItem],
) -> list[TResponseInputItem]:
    try:
        copied = json.loads(
            json.dumps(
                items,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    except (TypeError, ValueError):
        raise AgentSessionError(AgentSessionErrorCode.FAILURE) from None
    if (
        not isinstance(copied, list)
        or any(not isinstance(item, dict) for item in copied)
    ):
        raise AgentSessionError(AgentSessionErrorCode.FAILURE)
    return copied


def _digest(body: dict[str, object]) -> str:
    return f"sha256:{hashlib.sha256(_canonical(body)).hexdigest()}"


class FilesystemAgentSession:
    """Append-only local implementation of the Agents SDK Session protocol."""

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        root: AuthorizedRoot,
        *,
        session_id: str,
    ) -> None:
        if (
            not isinstance(root, AuthorizedRoot)
            or not isinstance(session_id, str)
            or not session_id
            or len(session_id.encode("utf-8")) > _MAX_SESSION_ID_BYTES
            or any(
                unicodedata.category(char).startswith("C")
                for char in session_id
            )
        ):
            raise AgentSessionError(AgentSessionErrorCode.FAILURE)
        self.root = root
        self.session_id = session_id
        self._items: list[TResponseInputItem] = []
        self._sequence = 0
        self._last_digest: str | None = None
        self._lock = threading.Lock()
        self._load()

    async def get_items(
        self,
        limit: int | None = None,
    ) -> list[TResponseInputItem]:
        if (
            limit is not None
            and (type(limit) is not int or limit < 0)
        ):
            raise AgentSessionError(AgentSessionErrorCode.FAILURE)
        with self._lock:
            selected = (
                self._items
                if limit is None
                else self._items[-limit:] if limit else []
            )
            return _copy_items(list(selected))

    async def add_items(
        self,
        items: list[TResponseInputItem],
    ) -> None:
        copied = _copy_items(items)
        if not copied:
            return
        self._append("add", copied)

    async def pop_item(self) -> TResponseInputItem | None:
        return self._append("pop", [])

    async def clear_session(self) -> None:
        self._append("clear", [])

    def _append(
        self,
        operation: str,
        items: list[TResponseInputItem],
    ) -> TResponseInputItem | None:
        with self._lock:
            expected = (
                _copy_items(self._items),
                self._sequence,
                self._last_digest,
            )
            self._load()
            if (
                self._items,
                self._sequence,
                self._last_digest,
            ) != expected:
                raise AgentSessionError(AgentSessionErrorCode.CONFLICT)
            next_items = list(self._items)
            popped: TResponseInputItem | None = None
            if operation == "add":
                if len(next_items) + len(items) > _MAX_ITEMS:
                    raise AgentSessionError(
                        AgentSessionErrorCode.LIMIT_EXCEEDED
                    )
                next_items.extend(items)
            elif operation == "pop":
                if not next_items:
                    return None
                popped = _copy_items([next_items.pop()])[0]
            elif operation == "clear":
                if not next_items:
                    return None
                next_items.clear()
            else:
                raise AgentSessionError(AgentSessionErrorCode.FAILURE)

            sequence = self._sequence + 1
            body: dict[str, object] = {
                "items": items,
                "operation": operation,
                "previous_digest": self._last_digest,
                "schema_version": _SCHEMA_VERSION,
                "sequence": sequence,
                "session_id": self.session_id,
            }
            digest = _digest(body)
            record = dict(body)
            record["record_digest"] = digest
            content = _canonical(record)
            if len(content) > _MAX_RECORD_BYTES:
                raise AgentSessionError(
                    AgentSessionErrorCode.LIMIT_EXCEEDED
                )
            root_fd = self._open_root()
            try:
                write_once_at(
                    root_fd,
                    self._name(sequence),
                    content,
                    limit=_MAX_RECORD_BYTES,
                )
            except ImmutableFileError as error:
                code = (
                    AgentSessionErrorCode.CONFLICT
                    if error.code is ImmutableFileErrorCode.EXISTS
                    else AgentSessionErrorCode.FAILURE
                )
                raise AgentSessionError(code) from None
            finally:
                os.close(root_fd)
            self._items = next_items
            self._sequence = sequence
            self._last_digest = digest
            return popped

    def _load(self) -> None:
        root_fd = self._open_root()
        try:
            try:
                names = os.listdir(root_fd)
            except OSError:
                raise AgentSessionError(
                    AgentSessionErrorCode.FAILURE
                ) from None
            sequences: list[int] = []
            for name in names:
                match = _FILE_NAME.fullmatch(name)
                if match is None:
                    raise AgentSessionError(
                        AgentSessionErrorCode.CORRUPT
                    )
                sequences.append(int(match.group(1)))
            sequences.sort()
            if sequences != list(range(1, len(sequences) + 1)):
                raise AgentSessionError(AgentSessionErrorCode.CORRUPT)

            items: list[TResponseInputItem] = []
            previous_digest: str | None = None
            for sequence in sequences:
                try:
                    content = read_at(
                        root_fd,
                        self._name(sequence),
                        limit=_MAX_RECORD_BYTES,
                    )
                except ImmutableFileError as error:
                    code = (
                        AgentSessionErrorCode.FAILURE
                        if error.code is ImmutableFileErrorCode.IO
                        else AgentSessionErrorCode.CORRUPT
                    )
                    raise AgentSessionError(code) from None
                operation, record_items, previous_digest = (
                    self._decode_record(
                        content,
                        sequence=sequence,
                        previous_digest=previous_digest,
                    )
                )
                if operation == "add":
                    if len(items) + len(record_items) > _MAX_ITEMS:
                        raise AgentSessionError(
                            AgentSessionErrorCode.CORRUPT
                        )
                    items.extend(record_items)
                elif operation == "pop":
                    if not items:
                        raise AgentSessionError(
                            AgentSessionErrorCode.CORRUPT
                        )
                    items.pop()
                elif operation == "clear":
                    items.clear()
            self._items = items
            self._sequence = len(sequences)
            self._last_digest = previous_digest
        finally:
            os.close(root_fd)

    def _decode_record(
        self,
        content: bytes,
        *,
        sequence: int,
        previous_digest: str | None,
    ) -> tuple[str, list[TResponseInputItem], str]:
        try:
            payload = json.loads(content)
            if (
                not isinstance(payload, dict)
                or frozenset(payload) != _FIELDS
                or payload["schema_version"] != _SCHEMA_VERSION
                or payload["session_id"] != self.session_id
                or payload["sequence"] != sequence
                or type(payload["sequence"]) is not int
                or payload["previous_digest"] != previous_digest
                or payload["operation"] not in {"add", "pop", "clear"}
                or not isinstance(payload["record_digest"], str)
                or _DIGEST.fullmatch(payload["record_digest"]) is None
                or not isinstance(payload["items"], list)
            ):
                raise ValueError
            operation = payload["operation"]
            if operation != "add" and payload["items"]:
                raise ValueError
            record_items = _copy_items(payload["items"])
            body = {key: payload[key] for key in _BODY_FIELDS}
            digest = _digest(body)
            if (
                not hmac.compare_digest(
                    digest,
                    payload["record_digest"],
                )
                or _canonical(payload) != content
            ):
                raise ValueError
            return operation, record_items, digest
        except (
            AgentSessionError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            raise AgentSessionError(AgentSessionErrorCode.CORRUPT) from None

    def _open_root(self) -> int:
        try:
            return open_root(self.root)
        except ImmutableFileError:
            raise AgentSessionError(AgentSessionErrorCode.FAILURE) from None

    @staticmethod
    def _name(sequence: int) -> str:
        return f"session-{sequence:08d}.json"
