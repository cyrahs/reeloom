from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    if not os.environ.get("REELOOM_TEST_POSTGRES_DSN"):
        sys.stderr.write(
            "REELOOM_TEST_POSTGRES_DSN must be set explicitly\n"
        )
        return 2
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
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
