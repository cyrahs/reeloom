from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "src"))

from reeloom.server.api import ApiDependencies, ApiQueries, create_api
from reeloom.server.auth import AuthSettings


def _canonical_schema() -> str:
    app = create_api(
        ApiDependencies(queries=cast(ApiQueries, object())),
        auth=AuthSettings.create(
            admin_token="snapshot-admin-token",
            allowed_hosts=("snapshot.invalid",),
            allowed_origins=("https://snapshot.invalid",),
        ),
    )
    return json.dumps(
        app.openapi(),
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=_REPOSITORY_ROOT / "docs" / "openapi-v1.json",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = _canonical_schema()
    if args.check:
        if not args.output.is_file():
            print(f"OpenAPI snapshot is missing: {args.output}", file=sys.stderr)
            return 1
        actual = args.output.read_text(encoding="utf-8")
        if actual == expected:
            return 0
        sys.stderr.writelines(
            difflib.unified_diff(
                actual.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=str(args.output),
                tofile="generated OpenAPI schema",
            )
        )
        return 1
    if args.output.exists():
        raise SystemExit("refusing to overwrite existing contract snapshot")
    args.output.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
