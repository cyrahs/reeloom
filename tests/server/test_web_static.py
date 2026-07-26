from __future__ import annotations

import json
from pathlib import Path

import pytest

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.web_static import StaticWebBundle


def _manifest(path: Path, asset: str) -> None:
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "index.html": {
                    "file": asset,
                    "isEntry": True,
                }
            }
        ),
        encoding="utf-8",
    )
    (path / "index.html").write_text("<main>Reeloom</main>", encoding="utf-8")


def test_bundle_rejects_manifest_traversal(tmp_path: Path) -> None:
    _manifest(tmp_path, "../private.js")

    with pytest.raises(ServerError) as raised:
        StaticWebBundle.load(tmp_path)

    assert raised.value.code is ServerErrorCode.UNSAFE_STATE_ROOT


def test_bundle_rejects_symlinked_asset(tmp_path: Path) -> None:
    outside = tmp_path / "outside.js"
    outside.write_text("secret", encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app-deadbeef.js").symlink_to(outside)
    _manifest(tmp_path, "assets/app-deadbeef.js")

    with pytest.raises(ServerError) as raised:
        StaticWebBundle.load(tmp_path)

    assert raised.value.code is ServerErrorCode.UNSAFE_STATE_ROOT
