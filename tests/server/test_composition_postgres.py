from __future__ import annotations

import os

import pytest

from reeloom.server.auth import AuthSettings
from reeloom.server.composition import ServerApplication, build_application
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.settings import DeploymentSettings


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


def test_application_close_releases_resources_after_background_failure(
    tmp_path,
) -> None:
    calls: list[str] = []

    class Background:
        def close(self) -> None:
            calls.append("background")
            raise RuntimeError("background failed")

    class Database:
        def stop_boot(self, boot_id: str) -> None:
            calls.append(f"stop:{boot_id}")

        def close(self) -> None:
            calls.append("database")

    class Lock:
        def close(self) -> None:
            calls.append("lock")

    application = ServerApplication(
        settings=DeploymentSettings(
            postgres_dsn="postgresql://reeloom@db/reeloom",
            state_root=tmp_path,
        ),
        boot_id="boot-test",
        process_lock=Lock(),  # type: ignore[arg-type]
        database=Database(),  # type: ignore[arg-type]
        api=object(),  # type: ignore[arg-type]
        background=Background(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="background failed"):
        application.close()

    assert calls == ["background", "stop:boot-test", "database", "lock"]
    assert application._closed
    application.close()
    assert calls == ["background", "stop:boot-test", "database", "lock"]


@pytest.mark.postgres
def test_production_builder_enforces_single_instance_and_closes(
    tmp_path,
) -> None:
    settings = DeploymentSettings(
        postgres_dsn=_dsn(),
        state_root=tmp_path,
        workers=1,
    )
    auth = AuthSettings.create(
        admin_token="admin-token-for-test",
        allowed_hosts=("reeloom.test",),
        allowed_origins=("https://ui.example.test",),
    )
    first = build_application(settings, auth=auth)
    try:
        with pytest.raises(ServerError) as raised:
            build_application(settings, auth=auth)
        assert raised.value.code is ServerErrorCode.INSTANCE_ALREADY_RUNNING
        assert first.database.health().postgres_major in {16, 17, 18}
    finally:
        first.close()

    replacement = build_application(settings, auth=auth)
    replacement.close()
