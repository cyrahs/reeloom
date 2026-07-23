from __future__ import annotations

import argparse
import asyncio
import logging
import os
import stat
from pathlib import Path

from reeloom.adapters.tmdb import TmdbHttpAdapter, TmdbHttpLimits
from reeloom.kernel.tmdb import TmdbLanguage, TmdbSearchCandidate, TmdbWorkType
from reeloom.ports.tmdb import TmdbProviderError

_FRIEREN_TMDB_ID = 209867
_BREAKING_BAD_TMDB_ID = 1396
_FIGHT_CLUB_TMDB_ID = 550
_ADULT_MOVIE_TMDB_ID = 1358188
_ADULT_MOVIE_QUERY = "My Neighbor's A Nudist?!"
_MAX_DOTENV_BYTES = 64 * 1024
_PROJECT_DOTENV_PATH = Path(__file__).absolute().parent.parent / ".env"

logger = logging.getLogger(__name__)


class LiveSmokeFailure(RuntimeError):
    """A stable, non-sensitive live-check failure."""


class LiveSmokeConfigurationError(RuntimeError):
    """A stable configuration failure that never includes secret data."""


def _validate_api_key(value: str) -> str:
    key = value.strip()
    if (
        not 1 <= len(key) <= 512
        or "$" in key
        or any(not 33 <= ord(character) <= 126 for character in key)
    ):
        raise LiveSmokeConfigurationError("invalid_api_key")
    return key


def _parse_dotenv_api_key(contents: str) -> str | None:
    found: str | None = None
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        if not separator or name.strip() != "TMDB_API_KEY":
            continue
        if found is not None:
            raise LiveSmokeConfigurationError("duplicate_tmdb_api_key")

        value = raw_value.strip()
        if value[:1] in {'"', "'"}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise LiveSmokeConfigurationError("invalid_dotenv_value")
            value = value[1:-1]
        found = _validate_api_key(value)
    return found


def _read_dotenv_api_key(path: Path) -> str | None:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise LiveSmokeConfigurationError("no_nofollow_support")
    flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
    try:
        file_descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise LiveSmokeConfigurationError("dotenv_open_failed") from None

    try:
        metadata = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_DOTENV_BYTES
        ):
            raise LiveSmokeConfigurationError("invalid_dotenv_file")
        contents = bytearray()
        while len(contents) <= _MAX_DOTENV_BYTES:
            chunk = os.read(
                file_descriptor,
                min(8_192, _MAX_DOTENV_BYTES + 1 - len(contents)),
            )
            if not chunk:
                break
            contents.extend(chunk)
        if len(contents) > _MAX_DOTENV_BYTES:
            raise LiveSmokeConfigurationError("dotenv_too_large")
    finally:
        os.close(file_descriptor)

    try:
        decoded = bytes(contents).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LiveSmokeConfigurationError("invalid_dotenv_encoding") from None
    return _parse_dotenv_api_key(decoded)


def _load_api_key() -> str | None:
    process_value = os.environ.get("TMDB_API_KEY", "")
    if process_value.strip():
        return _validate_api_key(process_value)
    return _read_dotenv_api_key(_PROJECT_DOTENV_PATH)


def _require_candidate(
    candidates: tuple[TmdbSearchCandidate, ...],
    *,
    expected_id: int,
    check: str,
) -> None:
    if not any(candidate.tmdb_id == expected_id for candidate in candidates):
        raise LiveSmokeFailure(f"{check}_expected_id_missing")


async def _run_live_smoke(
    api_key: str,
) -> tuple[int, int, int, int, int]:
    adapter = TmdbHttpAdapter(
        api_key=api_key,
        limits=TmdbHttpLimits(
            timeout_seconds=10.0,
            cache_ttl_seconds=0,
        ),
    )
    try:
        anime_candidates = await adapter.search_titles(
            query="Frieren",
            work_type=TmdbWorkType.ANIME,
            language=TmdbLanguage.ZH_CN,
            limit=20,
        )
        _require_candidate(
            anime_candidates,
            expected_id=_FRIEREN_TMDB_ID,
            check="anime_search",
        )

        series = await adapter.get_series(
            tmdb_id=_FRIEREN_TMDB_ID,
            work_type=TmdbWorkType.ANIME,
            language=TmdbLanguage.ZH_CN,
        )
        if not any(season.season_number == 1 for season in series.seasons):
            raise LiveSmokeFailure("tv_details_season_missing")

        season = await adapter.get_season(
            tmdb_id=_FRIEREN_TMDB_ID,
            work_type=TmdbWorkType.ANIME,
            season_number=1,
            language=TmdbLanguage.ZH_CN,
        )
        if not season.episodes:
            raise LiveSmokeFailure("season_details_episodes_missing")

        tv_candidates = await adapter.search_titles(
            query="Breaking Bad",
            work_type=TmdbWorkType.TV_SERIES,
            language=TmdbLanguage.EN_US,
            limit=20,
        )
        _require_candidate(
            tv_candidates,
            expected_id=_BREAKING_BAD_TMDB_ID,
            check="tv_search",
        )

        movie_candidates = await adapter.search_titles(
            query="Fight Club",
            work_type=TmdbWorkType.MOVIE,
            language=TmdbLanguage.EN_US,
            limit=20,
        )
        _require_candidate(
            movie_candidates,
            expected_id=_FIGHT_CLUB_TMDB_ID,
            check="movie_search",
        )

        adult_hidden_candidates = await adapter.search_titles(
            query=_ADULT_MOVIE_QUERY,
            work_type=TmdbWorkType.MOVIE,
            language=TmdbLanguage.EN_US,
            limit=20,
            include_adult=False,
        )
        if any(
            candidate.tmdb_id == _ADULT_MOVIE_TMDB_ID
            for candidate in adult_hidden_candidates
        ):
            raise LiveSmokeFailure("adult_search_not_hidden_when_disabled")

        adult_candidates = await adapter.search_titles(
            query=_ADULT_MOVIE_QUERY,
            work_type=TmdbWorkType.MOVIE,
            language=TmdbLanguage.EN_US,
            limit=20,
            include_adult=True,
        )
        _require_candidate(
            adult_candidates,
            expected_id=_ADULT_MOVIE_TMDB_ID,
            check="adult_search",
        )
        adult_metadata = await adapter.get_movie(
            tmdb_id=_ADULT_MOVIE_TMDB_ID,
            work_type=TmdbWorkType.MOVIE,
            language=TmdbLanguage.EN_US,
        )
        if not adult_metadata.adult:
            raise LiveSmokeFailure("adult_metadata_flag_missing")

        return (
            len(anime_candidates),
            len(tv_candidates),
            len(movie_candidates),
            len(season.episodes),
            len(adult_candidates),
        )
    finally:
        await adapter.aclose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an explicit live smoke check against TMDB API v3.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="confirm that real TMDB network access is intended",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.live:
        logger.error("live smoke disabled: pass --live to opt in")
        return 2

    try:
        api_key = _load_api_key()
    except LiveSmokeConfigurationError as error:
        logger.error("live smoke disabled: configuration=%s", error)
        return 2
    if not api_key:
        logger.error(
            "live smoke disabled: TMDB_API_KEY is absent from "
            "the process environment and project .env"
        )
        return 2

    try:
        (
            anime_count,
            tv_count,
            movie_count,
            episode_count,
            adult_count,
        ) = asyncio.run(_run_live_smoke(api_key))
    except TmdbProviderError as error:
        logger.error(
            "TMDB live smoke failed: code=%s retryable=%s",
            error.code.value,
            error.retryable,
        )
        return 1
    except LiveSmokeFailure as error:
        logger.error("TMDB live smoke failed: check=%s", error)
        return 1
    except Exception as error:
        logger.error(
            "TMDB live smoke failed: unexpected_type=%s",
            type(error).__name__,
        )
        return 1

    logger.info(
        "TMDB live smoke passed: anime_results=%d tv_results=%d "
        "movie_results=%d season_episodes=%d adult_results=%d "
        "adult_capability=available",
        anime_count,
        tv_count,
        movie_count,
        episode_count,
        adult_count,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
