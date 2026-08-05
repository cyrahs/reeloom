from reeloom.agents.organizer import (
    ANIME_ORGANIZER_INSTRUCTIONS,
    ANIME_ORGANIZER_TOOL_NAMES,
    EPISODE_ORGANIZER_INSTRUCTIONS,
    EPISODE_ORGANIZER_TOOL_NAMES,
    MOVIE_ORGANIZER_INSTRUCTIONS,
    MOVIE_ORGANIZER_TOOL_NAMES,
    M13_PROBE_ORGANIZER_TOOL_NAMES,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.server.agent_definition import AgentDefinitionRevision

ORGANIZER_NAME = "EpisodeOrganizerAgent"
LEGACY_ORGANIZER_SCHEMA_VERSION = "episode-organizer-v1"
V2_ORGANIZER_SCHEMA_VERSION = "episode-organizer-v2"
V3_ORGANIZER_SCHEMA_VERSION = "episode-organizer-v3"
V4_ORGANIZER_SCHEMA_VERSION = "episode-organizer-v4"
V5_ORGANIZER_SCHEMA_VERSION = "episode-organizer-v5"
PREVIOUS_ORGANIZER_SCHEMA_VERSION = "episode-organizer-v6"
ORGANIZER_SCHEMA_VERSION = "episode-organizer-v7"
TV_ORGANIZER_SCHEMA_VERSION = V4_ORGANIZER_SCHEMA_VERSION
MOVIE_ORGANIZER_NAME = "MovieOrganizerAgent"
LEGACY_MOVIE_ORGANIZER_SCHEMA_VERSION = "movie-organizer-v1"
V2_MOVIE_ORGANIZER_SCHEMA_VERSION = "movie-organizer-v2"
PREVIOUS_MOVIE_ORGANIZER_SCHEMA_VERSION = "movie-organizer-v3"
MOVIE_ORGANIZER_SCHEMA_VERSION = "movie-organizer-v4"
LEGACY_EPISODE_ORGANIZER_TOOL_NAMES = (
    "list_candidates",
    "search_tmdb",
    "get_tmdb_series",
    "get_tmdb_season",
    "select_series",
    "get_existing_inventory",
    "detect_subtitle_variant",
    "submit_mapping",
)
LEGACY_MOVIE_ORGANIZER_TOOL_NAMES = (
    "list_candidates",
    "search_tmdb",
    "get_tmdb_movie",
    "select_movie",
    "detect_subtitle_variant",
    "submit_mapping",
)


def organizer_definition(
    work_type: TmdbWorkType,
    *,
    subtitle_acquisition_enabled: bool = True,
) -> AgentDefinitionRevision:
    movie = work_type is TmdbWorkType.MOVIE
    anime = work_type is TmdbWorkType.ANIME
    instructions = (
        MOVIE_ORGANIZER_INSTRUCTIONS
        if movie
        else (
            ANIME_ORGANIZER_INSTRUCTIONS
            if anime and subtitle_acquisition_enabled
            else EPISODE_ORGANIZER_INSTRUCTIONS
        )
    )
    tools = (
        MOVIE_ORGANIZER_TOOL_NAMES
        if movie
        else (
            ANIME_ORGANIZER_TOOL_NAMES
            if anime and subtitle_acquisition_enabled
            else EPISODE_ORGANIZER_TOOL_NAMES
        )
    )
    return AgentDefinitionRevision.create(
        name=MOVIE_ORGANIZER_NAME if movie else ORGANIZER_NAME,
        instructions=(
            f"{instructions}\n"
            f"This run's authorized work_type is {work_type.value}."
        ),
        tools=tools,
        schema_version=(
            MOVIE_ORGANIZER_SCHEMA_VERSION
            if movie
            else (
                ORGANIZER_SCHEMA_VERSION
                if anime and subtitle_acquisition_enabled
                else TV_ORGANIZER_SCHEMA_VERSION
            )
        ),
    )


def is_supported_organizer_definition(
    definition: AgentDefinitionRevision,
    work_type: TmdbWorkType,
    *,
    allow_v1: bool,
) -> bool:
    movie = work_type is TmdbWorkType.MOVIE
    anime = work_type is TmdbWorkType.ANIME
    name = MOVIE_ORGANIZER_NAME if movie else ORGANIZER_NAME
    if movie:
        accepted = {
            (version, MOVIE_ORGANIZER_TOOL_NAMES)
            for version in (
                MOVIE_ORGANIZER_SCHEMA_VERSION,
                PREVIOUS_MOVIE_ORGANIZER_SCHEMA_VERSION,
                V2_MOVIE_ORGANIZER_SCHEMA_VERSION,
            )
        }
    else:
        accepted = {
            (
                ORGANIZER_SCHEMA_VERSION
                if anime
                else TV_ORGANIZER_SCHEMA_VERSION,
                (
                    ANIME_ORGANIZER_TOOL_NAMES
                    if anime
                    else EPISODE_ORGANIZER_TOOL_NAMES
                ),
            )
        }
        if anime:
            accepted.update(
                {
                    (
                        PREVIOUS_ORGANIZER_SCHEMA_VERSION,
                        ANIME_ORGANIZER_TOOL_NAMES,
                    ),
                    (
                        V5_ORGANIZER_SCHEMA_VERSION,
                        M13_PROBE_ORGANIZER_TOOL_NAMES,
                    ),
                }
            )
        accepted.update(
            (version, EPISODE_ORGANIZER_TOOL_NAMES)
            for version in (
                V4_ORGANIZER_SCHEMA_VERSION,
                V3_ORGANIZER_SCHEMA_VERSION,
                V2_ORGANIZER_SCHEMA_VERSION,
            )
        )
    if allow_v1:
        accepted.add(
            (
                (
                    LEGACY_MOVIE_ORGANIZER_SCHEMA_VERSION
                    if movie
                    else LEGACY_ORGANIZER_SCHEMA_VERSION
                ),
                (
                    LEGACY_MOVIE_ORGANIZER_TOOL_NAMES
                    if movie
                    else LEGACY_EPISODE_ORGANIZER_TOOL_NAMES
                ),
            )
        )
    expected = (definition.schema_version, definition.tools)
    return definition.name == name and expected in accepted
