from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from reeloom.server.api import ApiDependencies, ApiQueries, create_api
from reeloom.server.auth import AuthSettings, Role

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _canonical_schema() -> str:
    app = create_api(
        ApiDependencies(queries=cast(ApiQueries, object())),
        auth=AuthSettings.create(
            credentials={
                Role.ADMIN: "snapshot-admin-token",
                Role.OPERATOR: "snapshot-operator-token",
                Role.VIEWER: "snapshot-viewer-token",
            },
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
            return 1
        return 0 if args.output.read_text(encoding="utf-8") == expected else 1
    if args.output.exists():
        raise SystemExit("refusing to overwrite existing contract snapshot")
    args.output.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
