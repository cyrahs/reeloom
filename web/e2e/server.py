from __future__ import annotations

import os
from pathlib import Path

import psycopg
import uvicorn
from psycopg import sql
from psycopg.conninfo import make_conninfo

from reeloom.server.api import ApiDependencies, create_api
from reeloom.server.auth import AuthSettings, Role
from reeloom.server.database import PostgresControlPlane
from reeloom.server.queries import PostgresQueries


def main() -> None:
    dsn = os.environ.get("REELOOM_TEST_POSTGRES_DSN", "")
    if not dsn:
        raise SystemExit("REELOOM_TEST_POSTGRES_DSN must be set explicitly")
    schema = "reeloom_browser_e2e"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                sql.Identifier(schema)
            )
        )
    isolated_dsn = make_conninfo(
        dsn,
        options=f"-c search_path={schema}",
    )
    control = PostgresControlPlane(isolated_dsn)
    control.open()
    control.migrate()
    root = Path(__file__).resolve().parents[2]
    app = create_api(
        ApiDependencies(
            queries=PostgresQueries(control.pool),
            health=control.health,
            sse_max_empty_polls=1,
        ),
        auth=AuthSettings.create(
            credentials={
                Role.ADMIN: "admin-e2e-token-strong",
                Role.OPERATOR: "operator-e2e-token-strong",
                Role.VIEWER: "viewer-e2e-token-strong",
            },
            allowed_hosts=("127.0.0.1",),
            allowed_origins=("http://127.0.0.1:4173",),
        ),
        static_root=root / "src/reeloom/server/static",
    )
    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=4173,
            access_log=False,
            server_header=False,
        )
    finally:
        control.close()


if __name__ == "__main__":
    main()
