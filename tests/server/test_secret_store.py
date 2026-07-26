from __future__ import annotations

from pathlib import Path

import pytest

from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.secrets import FilesystemSecretStore


def test_secret_store_writes_once_and_never_exposes_value(
    tmp_path: Path,
) -> None:
    store = FilesystemSecretStore(AuthorizedRoot.create(tmp_path))

    reference = store.put(b"top-secret-api-key")

    assert "top-secret" not in reference
    assert "top-secret" not in repr(store)
    assert store.load(reference) == b"top-secret-api-key"
    stored = tuple(tmp_path.iterdir())
    assert len(stored) == 1
    assert stored[0].stat().st_mode & 0o777 == 0o600


def test_secret_store_rejects_symlink_and_unknown_reference(
    tmp_path: Path,
) -> None:
    store = FilesystemSecretStore(AuthorizedRoot.create(tmp_path))
    (tmp_path / "secret-v1-deadbeef").symlink_to(tmp_path / "missing")

    for reference in ("secret-v1-deadbeef", "../secret", "unknown"):
        with pytest.raises(ServerError) as raised:
            store.load(reference)
        assert raised.value.code in {
            ServerErrorCode.SECRET_NOT_FOUND,
            ServerErrorCode.INVALID_SECRET,
        }


def test_secret_bytes_are_bounded(tmp_path: Path) -> None:
    store = FilesystemSecretStore(AuthorizedRoot.create(tmp_path))

    with pytest.raises(ServerError) as raised:
        store.put(b"x" * 4097)

    assert raised.value.code is ServerErrorCode.INVALID_SECRET
