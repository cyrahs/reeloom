from reeloom.agents.organizer import (
    EPISODE_ORGANIZER_INSTRUCTIONS,
    EPISODE_ORGANIZER_TOOL_NAMES,
    MOVIE_ORGANIZER_INSTRUCTIONS,
    MOVIE_ORGANIZER_TOOL_NAMES,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.server.agent_definition import AgentDefinitionRevision

ORGANIZER_NAME = "EpisodeOrganizerAgent"
LEGACY_ORGANIZER_SCHEMA_VERSION = "episode-organizer-v1"
PREVIOUS_ORGANIZER_SCHEMA_VERSION = "episode-organizer-v2"
ORGANIZER_SCHEMA_VERSION = "episode-organizer-v3"
MOVIE_ORGANIZER_NAME = "MovieOrganizerAgent"
LEGACY_MOVIE_ORGANIZER_SCHEMA_VERSION = "movie-organizer-v1"
PREVIOUS_MOVIE_ORGANIZER_SCHEMA_VERSION = "movie-organizer-v2"
MOVIE_ORGANIZER_SCHEMA_VERSION = "movie-organizer-v3"
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
) -> AgentDefinitionRevision:
    movie = work_type is TmdbWorkType.MOVIE
    return AgentDefinitionRevision.create(
        name=MOVIE_ORGANIZER_NAME if movie else ORGANIZER_NAME,
        instructions=(
            f"{MOVIE_ORGANIZER_INSTRUCTIONS if movie else EPISODE_ORGANIZER_INSTRUCTIONS}\n"
            f"This run's authorized work_type is {work_type.value}."
        ),
        tools=(
            MOVIE_ORGANIZER_TOOL_NAMES
            if movie
            else EPISODE_ORGANIZER_TOOL_NAMES
        ),
        schema_version=(
            MOVIE_ORGANIZER_SCHEMA_VERSION
            if movie
            else ORGANIZER_SCHEMA_VERSION
        ),
    )


def is_supported_organizer_definition(
    definition: AgentDefinitionRevision,
    work_type: TmdbWorkType,
    *,
    allow_v1: bool,
) -> bool:
    movie = work_type is TmdbWorkType.MOVIE
    name = MOVIE_ORGANIZER_NAME if movie else ORGANIZER_NAME
    tools = (
        MOVIE_ORGANIZER_TOOL_NAMES
        if movie
        else EPISODE_ORGANIZER_TOOL_NAMES
    )
    versions = (
        (
            MOVIE_ORGANIZER_SCHEMA_VERSION,
            PREVIOUS_MOVIE_ORGANIZER_SCHEMA_VERSION,
        )
        if movie
        else (
            ORGANIZER_SCHEMA_VERSION,
            PREVIOUS_ORGANIZER_SCHEMA_VERSION,
        )
    )
    accepted = {(version, tools) for version in versions}
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
