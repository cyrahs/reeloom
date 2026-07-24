from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import unicodedata

from reeloom.adapters._immutable_file import (
    ImmutableFileError,
    ImmutableFileErrorCode,
    open_root,
    read_at,
    write_once_at,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.event_codec import decode_event, encode_event
from reeloom.runtime.events import RunStarted, RuntimeEvent
from reeloom.runtime.reducer import reduce_event
from reeloom.runtime.state import RunState
from reeloom.runtime.store import StoredEvent

_SCHEMA_VERSION = "runtime-event-record-v1"
_EVENT_NAME = re.compile(r"^event-([0-9]{8})\.json$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_RECORD_BYTES = 12 * 1024 * 1024
_MAX_RUN_ID_BYTES = 128
_BODY_FIELDS = frozenset(
    {"event", "previous_digest", "run_id", "schema_version", "sequence"}
)
_RECORD_FIELDS = _BODY_FIELDS | {"record_digest"}


def _error(code: RuntimeErrorCode) -> RuntimeDomainError:
    return RuntimeDomainError(code)


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _record_digest(body: dict[str, object]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(body)).hexdigest()}"


def _encode_record(
    *,
    run_id: str,
    sequence: int,
    previous_digest: str | None,
    event: RuntimeEvent,
) -> tuple[bytes, str]:
    body: dict[str, object] = {
        "event": json.loads(encode_event(event)),
        "previous_digest": previous_digest,
        "run_id": run_id,
        "schema_version": _SCHEMA_VERSION,
        "sequence": sequence,
    }
    digest = _record_digest(body)
    record = dict(body)
    record["record_digest"] = digest
    content = _canonical_json(record)
    if len(content) > _MAX_RECORD_BYTES:
        raise _error(RuntimeErrorCode.EVENT_STORE_FAILURE)
    return content, digest


def _decode_record(
    content: bytes,
    *,
    run_id: str,
    sequence: int,
    previous_digest: str | None,
) -> tuple[RuntimeEvent, str]:
    try:
        payload = json.loads(content)
        if (
            not isinstance(payload, dict)
            or frozenset(payload) != _RECORD_FIELDS
            or payload["schema_version"] != _SCHEMA_VERSION
            or payload["run_id"] != run_id
            or type(payload["sequence"]) is not int
            or payload["sequence"] != sequence
            or payload["previous_digest"] != previous_digest
            or not isinstance(payload["event"], dict)
            or not isinstance(payload["record_digest"], str)
            or _DIGEST.fullmatch(payload["record_digest"]) is None
        ):
            raise ValueError
        body = {key: payload[key] for key in _BODY_FIELDS}
        digest = _record_digest(body)
        if not hmac.compare_digest(digest, payload["record_digest"]):
            raise ValueError
        event = decode_event(_canonical_json(payload["event"]))
        canonical, _ = _encode_record(
            run_id=run_id,
            sequence=sequence,
            previous_digest=previous_digest,
            event=event,
        )
        if canonical != content:
            raise ValueError
        return event, digest
    except (
        json.JSONDecodeError,
        TypeError,
        UnicodeEncodeError,
        ValueError,
    ):
        raise _error(RuntimeErrorCode.EVENT_STORE_CORRUPT) from None
    except RuntimeDomainError as error:
        if error.code is RuntimeErrorCode.INVALID_EVENT:
            raise _error(RuntimeErrorCode.EVENT_STORE_CORRUPT) from None
        raise


class FilesystemEventStore:
    """No-follow, append-only event checkpoints for one authorized run root."""

    def __init__(self, root: AuthorizedRoot, *, run_id: str) -> None:
        if (
            not isinstance(root, AuthorizedRoot)
            or not isinstance(run_id, str)
            or not run_id
            or len(run_id.encode("utf-8")) > _MAX_RUN_ID_BYTES
            or any(
                unicodedata.category(character).startswith("C")
                for character in run_id
            )
        ):
            raise _error(RuntimeErrorCode.EVENT_STORE_FAILURE)
        self.root = root
        self.run_id = run_id
        self._events: list[StoredEvent] = []
        self._state: RunState | None = None
        self._last_digest: str | None = None
        self._lock = threading.Lock()
        self._load()

    @property
    def state(self) -> RunState | None:
        return self._state

    @property
    def events(self) -> tuple[StoredEvent, ...]:
        return tuple(self._events)

    def append(self, event: RuntimeEvent) -> RunState:
        with self._lock:
            expected = (
                tuple(self._events),
                self._state,
                self._last_digest,
            )
            self._load()
            if (
                tuple(self._events),
                self._state,
                self._last_digest,
            ) != expected:
                raise _error(RuntimeErrorCode.EVENT_STORE_CONFLICT)
            sequence = len(self._events) + 1
            if sequence == 1 and (
                not isinstance(event, RunStarted)
                or event.run_id != self.run_id
            ):
                raise _error(RuntimeErrorCode.RUN_ID_MISMATCH)
            next_state = reduce_event(self._state, event)
            content, digest = _encode_record(
                run_id=self.run_id,
                sequence=sequence,
                previous_digest=self._last_digest,
                event=event,
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
                if error.code is ImmutableFileErrorCode.EXISTS:
                    raise _error(
                        RuntimeErrorCode.EVENT_STORE_CONFLICT
                    ) from None
                raise _error(
                    RuntimeErrorCode.EVENT_STORE_FAILURE
                ) from None
            finally:
                os.close(root_fd)
            self._events.append(StoredEvent(sequence, event))
            self._state = next_state
            self._last_digest = digest
            return next_state

    def replay(self) -> RunState | None:
        state: RunState | None = None
        for stored in self._events:
            state = reduce_event(state, stored.event)
        return state

    def _load(self) -> None:
        root_fd = self._open_root()
        try:
            try:
                names = os.listdir(root_fd)
            except OSError:
                raise _error(RuntimeErrorCode.EVENT_STORE_FAILURE) from None
            sequences: list[int] = []
            for name in names:
                match = _EVENT_NAME.fullmatch(name)
                if match is None:
                    raise _error(RuntimeErrorCode.EVENT_STORE_CORRUPT)
                sequences.append(int(match.group(1)))
            sequences.sort()
            if sequences != list(range(1, len(sequences) + 1)):
                raise _error(RuntimeErrorCode.EVENT_STORE_CORRUPT)

            state: RunState | None = None
            previous_digest: str | None = None
            events: list[StoredEvent] = []
            for sequence in sequences:
                try:
                    content = read_at(
                        root_fd,
                        self._name(sequence),
                        limit=_MAX_RECORD_BYTES,
                    )
                except ImmutableFileError as error:
                    code = (
                        RuntimeErrorCode.EVENT_STORE_FAILURE
                        if error.code is ImmutableFileErrorCode.IO
                        else RuntimeErrorCode.EVENT_STORE_CORRUPT
                    )
                    raise _error(code) from None
                event, previous_digest = _decode_record(
                    content,
                    run_id=self.run_id,
                    sequence=sequence,
                    previous_digest=previous_digest,
                )
                if sequence == 1 and (
                    not isinstance(event, RunStarted)
                    or event.run_id != self.run_id
                ):
                    raise _error(RuntimeErrorCode.EVENT_STORE_CORRUPT)
                try:
                    state = reduce_event(state, event)
                except RuntimeDomainError:
                    raise _error(RuntimeErrorCode.EVENT_STORE_CORRUPT) from None
                events.append(StoredEvent(sequence, event))
            self._events = events
            self._state = state
            self._last_digest = previous_digest
        finally:
            os.close(root_fd)

    def _open_root(self) -> int:
        try:
            return open_root(self.root)
        except ImmutableFileError:
            raise _error(RuntimeErrorCode.EVENT_STORE_FAILURE) from None

    @staticmethod
    def _name(sequence: int) -> str:
        return f"event-{sequence:08d}.json"
