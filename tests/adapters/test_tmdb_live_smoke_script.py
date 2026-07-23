from __future__ import annotations

import asyncio
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

from scripts import tmdb_live_smoke


def _unexpected_run(_: object) -> NoReturn:
    pytest.fail("live smoke attempted to run")


def test_live_smoke_requires_explicit_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tmdb_live_smoke,
        "_parse_args",
        lambda: Namespace(live=False),
    )
    monkeypatch.setattr(
        tmdb_live_smoke,
        "_load_api_key",
        lambda: _unexpected_run(object()),
    )
    monkeypatch.setattr(tmdb_live_smoke.asyncio, "run", _unexpected_run)

    assert tmdb_live_smoke.main() == 2


def test_live_smoke_requires_api_key_from_allowed_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tmdb_live_smoke,
        "_parse_args",
        lambda: Namespace(live=True),
    )
    monkeypatch.setattr(tmdb_live_smoke, "_load_api_key", lambda: None)
    monkeypatch.setattr(tmdb_live_smoke.asyncio, "run", _unexpected_run)

    assert tmdb_live_smoke.main() == 2


def test_dotenv_loader_reads_only_tmdb_api_key(tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "IGNORED=value\nexport TMDB_API_KEY='test-key-not-secret'\n",
        encoding="utf-8",
    )

    assert (
        tmdb_live_smoke._read_dotenv_api_key(dotenv_path)
        == "test-key-not-secret"
    )


def test_dotenv_loader_rejects_duplicate_key(tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "TMDB_API_KEY=first\nTMDB_API_KEY=second\n",
        encoding="utf-8",
    )

    with pytest.raises(tmdb_live_smoke.LiveSmokeConfigurationError):
        tmdb_live_smoke._read_dotenv_api_key(dotenv_path)


def test_dotenv_loader_rejects_variable_expansion(tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "TMDB_API_KEY=${OTHER_SECRET}\n",
        encoding="utf-8",
    )

    with pytest.raises(tmdb_live_smoke.LiveSmokeConfigurationError):
        tmdb_live_smoke._read_dotenv_api_key(dotenv_path)


def test_dotenv_loader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "credentials"
    target.write_text("TMDB_API_KEY=test-key-not-secret\n", encoding="utf-8")
    dotenv_path = tmp_path / ".env"
    dotenv_path.symlink_to(target)

    with pytest.raises(tmdb_live_smoke.LiveSmokeConfigurationError):
        tmdb_live_smoke._read_dotenv_api_key(dotenv_path)


def test_process_environment_takes_precedence_over_dotenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMDB_API_KEY", "process-key")
    monkeypatch.setattr(
        tmdb_live_smoke,
        "_read_dotenv_api_key",
        _unexpected_run,
    )

    assert tmdb_live_smoke._load_api_key() == "process-key"


def test_api_key_loader_falls_back_to_project_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "TMDB_API_KEY=dotenv-key\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(
        tmdb_live_smoke,
        "_PROJECT_DOTENV_PATH",
        dotenv_path,
    )

    assert tmdb_live_smoke._load_api_key() == "dotenv-key"


def test_live_smoke_checks_adult_search_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool]] = []

    class FakeAdapter:
        closed = False

        async def search_titles(
            self,
            *,
            query: str,
            work_type: object,
            language: object,
            limit: int,
            include_adult: bool = True,
        ) -> tuple[SimpleNamespace, ...]:
            del work_type, language, limit
            calls.append((query, include_adult))
            expected_ids = {
                "Frieren": 209867,
                "Breaking Bad": 1396,
                "Fight Club": 550,
            }
            if query == tmdb_live_smoke._ADULT_MOVIE_QUERY:
                if not include_adult:
                    return ()
                return (
                    SimpleNamespace(
                        tmdb_id=tmdb_live_smoke._ADULT_MOVIE_TMDB_ID
                    ),
                )
            return (SimpleNamespace(tmdb_id=expected_ids[query]),)

        async def get_series(self, **kwargs: object) -> SimpleNamespace:
            del kwargs
            return SimpleNamespace(
                seasons=(SimpleNamespace(season_number=1),)
            )

        async def get_season(self, **kwargs: object) -> SimpleNamespace:
            del kwargs
            return SimpleNamespace(episodes=(object(),))

        async def get_movie(self, **kwargs: object) -> SimpleNamespace:
            del kwargs
            return SimpleNamespace(adult=True)

        async def aclose(self) -> None:
            self.closed = True

    adapter = FakeAdapter()
    monkeypatch.setattr(
        tmdb_live_smoke,
        "TmdbHttpAdapter",
        lambda **kwargs: adapter,
    )

    result = asyncio.run(
        tmdb_live_smoke._run_live_smoke("test-key-not-secret")
    )

    assert result == (1, 1, 1, 1, 1)
    assert (
        tmdb_live_smoke._ADULT_MOVIE_QUERY,
        False,
    ) in calls
    assert (
        tmdb_live_smoke._ADULT_MOVIE_QUERY,
        True,
    ) in calls
    assert adapter.closed
