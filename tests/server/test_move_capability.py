from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

import reeloom.server.move_capability as capability_module
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.move_capability import (
    MoveCapabilityStatus,
    probe_move_capability,
)


def test_probe_verifies_no_replace_and_cleans_owned_directories(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()

    result = probe_move_capability(
        AuthorizedRoot.create(source),
        AuthorizedRoot.create(destination),
    )

    assert result.status is MoveCapabilityStatus.SUPPORTED
    assert result.failure_code is None
    assert tuple(source.iterdir()) == ()
    assert tuple(destination.iterdir()) == ()


def test_probe_reports_unsupported_without_touching_other_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    existing = destination / "existing"
    existing.mkdir()

    def unsupported(*args: object) -> None:
        del args
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(
        capability_module,
        "rename_noreplace",
        unsupported,
    )
    result = probe_move_capability(
        AuthorizedRoot.create(source),
        AuthorizedRoot.create(destination),
    )

    assert result.status is MoveCapabilityStatus.UNSUPPORTED
    assert result.failure_code == "atomic_move_unsupported"
    assert tuple(source.iterdir()) == ()
    assert tuple(destination.iterdir()) == (existing,)


def test_probe_rejects_backend_that_ignores_no_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()

    def unsafe_rename(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        os.rename(
            source_name,
            destination_name,
            src_dir_fd=source_fd,
            dst_dir_fd=destination_fd,
        )

    monkeypatch.setattr(
        capability_module,
        "rename_noreplace",
        unsafe_rename,
    )
    result = probe_move_capability(
        AuthorizedRoot.create(source),
        AuthorizedRoot.create(destination),
    )

    assert result.status is MoveCapabilityStatus.UNSUPPORTED
    assert tuple(source.iterdir()) == ()
    assert tuple(destination.iterdir()) == ()


def test_probe_reports_uncertain_when_owned_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()

    def cleanup_failed(*args: object) -> None:
        del args
        raise OSError(errno.EIO, "cleanup failed")

    monkeypatch.setattr(
        capability_module,
        "_remove_owned_empty",
        cleanup_failed,
    )
    result = probe_move_capability(
        AuthorizedRoot.create(source),
        AuthorizedRoot.create(destination),
    )

    assert result.status is MoveCapabilityStatus.UNCERTAIN
    assert result.failure_code == "probe_cleanup_failed"


def test_probe_reports_permission_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()

    def denied(*args: object) -> tuple[int, int]:
        del args
        raise OSError(errno.EACCES, "untrusted backend text")

    monkeypatch.setattr(capability_module, "_mkdir_owned", denied)
    result = probe_move_capability(
        AuthorizedRoot.create(source),
        AuthorizedRoot.create(destination),
    )

    assert result.status is MoveCapabilityStatus.UNCERTAIN
    assert result.failure_code == "permission_denied"
