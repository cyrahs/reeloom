from __future__ import annotations

import os
import subprocess
import sys
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


def main() -> int:
    dsn = os.environ.get("REELOOM_TEST_POSTGRES_DSN", "")
    if not dsn:
        sys.stderr.write(
            "REELOOM_TEST_POSTGRES_DSN must be set explicitly\n"
        )
        return 2
    schema = f"reeloom_test_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    isolated_dsn = make_conninfo(
        dsn,
        options=f"-c search_path={schema}",
    )
    environ = dict(os.environ)
    environ["REELOOM_TEST_POSTGRES_DSN"] = isolated_dsn
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            "postgres",
        ],
        check=False,
        env=environ,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
