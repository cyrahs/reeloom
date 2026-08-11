from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from reeloom.server.auth import AuthSettings
from reeloom.server.composition import ServerApplication, build_application
from reeloom.server.database import PostgresControlPlane
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
        assert first.background.legacy_effects_enabled is False
        assert first.background.subtitle_acquisitions is not None
        assert first.background.forward_execution is not None
        assert (
            first.background.subtitle_acquisitions._operations
            is first.background.forward_execution._operations
        )
        assert not hasattr(first.background, "subtitle_successors")
        assert not hasattr(first.background, "subtitle_scans")
        with pytest.raises(ServerError) as raised:
            build_application(settings, auth=auth)
        assert raised.value.code is ServerErrorCode.INSTANCE_ALREADY_RUNNING
        assert first.database.health().postgres_major in {16, 17, 18}
    finally:
        first.close()

    replacement = build_application(settings, auth=auth)
    replacement.close()


@pytest.mark.postgres
def test_production_builder_checks_instance_lock_before_migration(
    tmp_path,
) -> None:
    schema = "composition_lock_order_" + uuid.uuid4().hex
    dsn = _dsn()
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    isolated_dsn = make_conninfo(dsn, options=f"-c search_path={schema}")
    running = PostgresControlPlane(isolated_dsn)
    state_root = tmp_path / "second-instance"
    state_root.mkdir(mode=0o700)
    try:
        running.open()
        running.acquire_instance_lock()

        with pytest.raises(ServerError) as raised:
            build_application(
                DeploymentSettings(
                    postgres_dsn=isolated_dsn,
                    state_root=state_root,
                    workers=1,
                ),
                auth=AuthSettings.create(
                    admin_token="admin-token-for-test",
                    allowed_hosts=("reeloom.test",),
                    allowed_origins=("https://ui.example.test",),
                ),
            )

        assert raised.value.code is ServerErrorCode.INSTANCE_ALREADY_RUNNING
        with psycopg.connect(isolated_dsn) as connection:
            assert connection.execute(
                "SELECT to_regclass('schema_migrations')"
            ).fetchone() == (None,)
    finally:
        running.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
