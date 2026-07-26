from __future__ import annotations

import ast
from pathlib import Path


def test_postgres_runner_uses_only_explicit_test_dsn() -> None:
    script = Path("scripts/run_postgres_tests.py")
    tree = ast.parse(script.read_text(encoding="utf-8"))
    source = script.read_text(encoding="utf-8")

    assert "REELOOM_TEST_POSTGRES_DSN" in source
    assert '".env' not in source
    assert "'.env" not in source
    assert "load_dotenv" not in source
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"eval", "exec"}
        for node in ast.walk(tree)
    )
