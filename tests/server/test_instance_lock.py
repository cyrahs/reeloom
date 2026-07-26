from __future__ import annotations

from pathlib import Path

import pytest

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.instance_lock import ProcessLock


def test_process_lock_rejects_second_instance(tmp_path: Path) -> None:
    first = ProcessLock.acquire(tmp_path)
    try:
        with pytest.raises(ServerError) as raised:
            ProcessLock.acquire(tmp_path)
        assert raised.value.code is ServerErrorCode.INSTANCE_ALREADY_RUNNING
    finally:
        first.close()

    replacement = ProcessLock.acquire(tmp_path)
    replacement.close()


def test_process_lock_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"")
    (tmp_path / "server.lock").symlink_to(target)

    with pytest.raises(ServerError) as raised:
        ProcessLock.acquire(tmp_path)

    assert raised.value.code is ServerErrorCode.UNSAFE_STATE_ROOT


def test_process_lock_rejects_group_or_world_writable_root(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o777)

    with pytest.raises(ServerError) as raised:
        ProcessLock.acquire(tmp_path)

    assert raised.value.code is ServerErrorCode.UNSAFE_STATE_ROOT
