from reeloom.agents.organizer import (
    EPISODE_ORGANIZER_INSTRUCTIONS,
    EPISODE_ORGANIZER_TOOL_NAMES,
    MOVIE_ORGANIZER_INSTRUCTIONS,
    MOVIE_ORGANIZER_TOOL_NAMES,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.server.agent_definition import AgentDefinitionRevision

ORGANIZER_NAME = "EpisodeOrganizerAgent"
ORGANIZER_SCHEMA_VERSION = "episode-organizer-v1"
MOVIE_ORGANIZER_NAME = "MovieOrganizerAgent"
MOVIE_ORGANIZER_SCHEMA_VERSION = "movie-organizer-v1"


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
