from __future__ import annotations

from pathlib import Path

import pytest

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.policy.path_policy import (
    AuthorizedRoot,
    is_forbidden_env_name,
    validate_relative_path,
)


@pytest.mark.parametrize(
    "value",
    (
        "/absolute/video.mkv",
        "../outside.mkv",
        "nested/../../outside.mkv",
        "nested//video.mkv",
        "nested/./video.mkv",
        r"C:\media\video.mkv",
        r"nested\video.mkv",
        "",
    ),
)
def test_relative_path_rejects_escape_forms(value: str) -> None:
    with pytest.raises(DomainError) as error:
        validate_relative_path(value)

    assert error.value.code is ErrorCode.PATH_ESCAPE


@pytest.mark.parametrize(
    "name",
    (".env", ".env.local", ".ENV", ".Env-secrets"),
)
def test_env_name_matching_is_case_insensitive(name: str) -> None:
    assert is_forbidden_env_name(name)


def test_env_path_is_rejected_before_filesystem_lookup() -> None:
    with pytest.raises(DomainError) as error:
        validate_relative_path("nested/.env.production/video.mkv")

    assert error.value.code is ErrorCode.ENV_PATH_FORBIDDEN


def test_env_root_is_rejected_before_filesystem_lookup(
    tmp_path: Path,
) -> None:
    with pytest.raises(DomainError) as error:
        AuthorizedRoot.create(tmp_path / ".env-secrets")

    assert error.value.code is ErrorCode.ENV_PATH_FORBIDDEN


def test_authorized_root_must_be_absolute(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(DomainError) as error:
        AuthorizedRoot.create(Path("relative/root"))

    assert error.value.code is ErrorCode.PATH_NOT_ABSOLUTE


def test_authorized_root_rejects_a_symlinked_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    root = real_parent / "media"
    root.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(DomainError) as error:
        AuthorizedRoot.create(alias / "media")

    assert error.value.code is ErrorCode.SYMLINK_NOT_ALLOWED

