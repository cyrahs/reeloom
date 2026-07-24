from __future__ import annotations

import json
from pathlib import Path

import pytest

from reeloom.adapters.event_store import FilesystemEventStore
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.events import (
    CandidateSnapshotCreated,
    RunStarted,
)
from reeloom.runtime.state import Phase


def _root(tmp_path: Path) -> AuthorizedRoot:
    path = tmp_path / "events"
    path.mkdir()
    return AuthorizedRoot.create(path)


def _started_store(root: AuthorizedRoot) -> FilesystemEventStore:
    store = FilesystemEventStore(root, run_id="run-m7")
    store.append(RunStarted("run-m7", TmdbWorkType.ANIME))
    store.append(CandidateSnapshotCreated("snapshot:1", 0))
    return store


def test_event_store_recovers_state_after_restart(tmp_path: Path) -> None:
    root = _root(tmp_path)
    original = _started_store(root)

    recovered = FilesystemEventStore(root, run_id="run-m7")

    assert recovered.events == original.events
    assert recovered.state == original.state
    assert recovered.replay() == recovered.state
    assert recovered.state is not None
    assert recovered.state.phase is Phase.IDENTIFY_SERIES


def test_event_record_embeds_one_structured_event_envelope(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    store = FilesystemEventStore(root, run_id="run-m7")
    store.append(RunStarted("run-m7", TmdbWorkType.ANIME))

    record = json.loads(
        (root.path / "event-00000001.json").read_bytes()
    )

    assert isinstance(record["event"], dict)
    assert record["event"]["event_type"] == "run_started"


def test_event_store_rejects_tamper_and_sequence_gaps(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _started_store(root)
    second = root.path / "event-00000002.json"
    second.write_bytes(second.read_bytes().replace(b"snapshot:1", b"snapshot:2"))

    with pytest.raises(RuntimeDomainError) as raised:
        FilesystemEventStore(root, run_id="run-m7")
    assert raised.value.code is RuntimeErrorCode.EVENT_STORE_CORRUPT

    second.rename(root.path / "event-00000003.json")
    with pytest.raises(RuntimeDomainError) as raised:
        FilesystemEventStore(root, run_id="run-m7")
    assert raised.value.code is RuntimeErrorCode.EVENT_STORE_CORRUPT


def test_event_store_does_not_follow_event_symlink(tmp_path: Path) -> None:
    root = _root(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    (root.path / "event-00000001.json").symlink_to(outside)

    with pytest.raises(RuntimeDomainError) as raised:
        FilesystemEventStore(root, run_id="run-m7")

    assert raised.value.code is RuntimeErrorCode.EVENT_STORE_FAILURE
    assert outside.read_bytes() == b"outside"


def test_stale_writer_cannot_overwrite_the_next_event(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first = FilesystemEventStore(root, run_id="run-m7")
    second = FilesystemEventStore(root, run_id="run-m7")
    first.append(RunStarted("run-m7", TmdbWorkType.ANIME))

    with pytest.raises(RuntimeDomainError) as raised:
        second.append(RunStarted("run-m7", TmdbWorkType.ANIME))

    assert raised.value.code is RuntimeErrorCode.EVENT_STORE_CONFLICT
    assert len(tuple(root.path.iterdir())) == 1


def test_invalid_transition_is_not_persisted(tmp_path: Path) -> None:
    root = _root(tmp_path)
    store = FilesystemEventStore(root, run_id="run-m7")
    store.append(RunStarted("run-m7", TmdbWorkType.ANIME))

    with pytest.raises(RuntimeDomainError):
        store.append(RunStarted("run-m7", TmdbWorkType.ANIME))

    assert len(store.events) == 1
    assert len(tuple(root.path.iterdir())) == 1


def test_live_event_store_detects_prior_record_tamper(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    store = FilesystemEventStore(root, run_id="run-m7")
    store.append(RunStarted("run-m7", TmdbWorkType.ANIME))
    first = root.path / "event-00000001.json"
    first.write_bytes(first.read_bytes().replace(b"run-m7", b"run-x7"))

    with pytest.raises(RuntimeDomainError) as raised:
        store.append(CandidateSnapshotCreated("snapshot:1", 0))

    assert raised.value.code is RuntimeErrorCode.EVENT_STORE_CORRUPT
    assert not (root.path / "event-00000002.json").exists()
