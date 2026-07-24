from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reeloom.adapters.agent_session import (
    AgentSessionError,
    AgentSessionErrorCode,
    FilesystemAgentSession,
)
from reeloom.policy.path_policy import AuthorizedRoot


def _root(tmp_path: Path) -> AuthorizedRoot:
    path = tmp_path / "session"
    path.mkdir()
    return AuthorizedRoot.create(path)


def test_agent_session_replays_add_pop_and_clear(tmp_path: Path) -> None:
    root = _root(tmp_path)
    session = FilesystemAgentSession(root, session_id="run-m7")
    first = {"role": "user", "content": "untrusted prompt"}
    second = {"role": "assistant", "content": "untrusted answer"}

    asyncio.run(session.add_items([first, second]))  # type: ignore[list-item]
    assert asyncio.run(session.pop_item()) == second
    asyncio.run(session.add_items([second]))  # type: ignore[list-item]
    assert asyncio.run(session.get_items(limit=1)) == [second]
    asyncio.run(session.clear_session())

    restarted = FilesystemAgentSession(root, session_id="run-m7")
    assert asyncio.run(restarted.get_items()) == []
    assert len(tuple(root.path.iterdir())) == 4


def test_agent_session_rejects_tamper_and_gap(tmp_path: Path) -> None:
    root = _root(tmp_path)
    session = FilesystemAgentSession(root, session_id="run-m7")
    asyncio.run(
        session.add_items(  # type: ignore[list-item]
            [{"role": "user", "content": "first"}]
        )
    )
    first = root.path / "session-00000001.json"
    first.write_bytes(first.read_bytes().replace(b"first", b"other"))

    with pytest.raises(AgentSessionError) as raised:
        FilesystemAgentSession(root, session_id="run-m7")
    assert raised.value.code is AgentSessionErrorCode.CORRUPT

    first.rename(root.path / "session-00000002.json")
    with pytest.raises(AgentSessionError) as raised:
        FilesystemAgentSession(root, session_id="run-m7")
    assert raised.value.code is AgentSessionErrorCode.CORRUPT


def test_agent_session_does_not_follow_symlink(tmp_path: Path) -> None:
    root = _root(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    (root.path / "session-00000001.json").symlink_to(outside)

    with pytest.raises(AgentSessionError) as raised:
        FilesystemAgentSession(root, session_id="run-m7")

    assert raised.value.code is AgentSessionErrorCode.FAILURE
    assert outside.read_bytes() == b"outside"


def test_stale_session_writer_cannot_overwrite(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first = FilesystemAgentSession(root, session_id="run-m7")
    second = FilesystemAgentSession(root, session_id="run-m7")
    item = {"role": "user", "content": "first"}
    asyncio.run(first.add_items([item]))  # type: ignore[list-item]

    with pytest.raises(AgentSessionError) as raised:
        asyncio.run(second.add_items([item]))  # type: ignore[list-item]

    assert raised.value.code is AgentSessionErrorCode.CONFLICT


def test_live_session_detects_prior_record_tamper(tmp_path: Path) -> None:
    root = _root(tmp_path)
    session = FilesystemAgentSession(root, session_id="run-m7")
    item = {"role": "user", "content": "first"}
    asyncio.run(session.add_items([item]))  # type: ignore[list-item]
    first = root.path / "session-00000001.json"
    first.write_bytes(first.read_bytes().replace(b"first", b"other"))

    with pytest.raises(AgentSessionError) as raised:
        asyncio.run(session.add_items([item]))  # type: ignore[list-item]

    assert raised.value.code is AgentSessionErrorCode.CORRUPT
    assert not (root.path / "session-00000002.json").exists()
