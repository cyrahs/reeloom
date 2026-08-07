from __future__ import annotations

from pathlib import Path

import pytest

from scripts.m14_filesystem_smoke import run


def test_m14_filesystem_smoke_converges_in_throwaway_directory(
    tmp_path: Path,
) -> None:
    result = run(tmp_path)

    assert result["result"] == "completed"
    assert result["backend"] in {"native", "checked_rename"}
    assert Path(str(result["smoke_directory"])).is_dir()


def test_m14_filesystem_smoke_refuses_nonempty_scope(tmp_path: Path) -> None:
    (tmp_path / "user-file").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="must be empty"):
        run(tmp_path)

    assert (tmp_path / "user-file").read_text(encoding="utf-8") == "keep"
