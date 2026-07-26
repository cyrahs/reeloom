from reeloom.agents.organizer import (
    EPISODE_ORGANIZER_INSTRUCTIONS,
    EPISODE_ORGANIZER_TOOL_NAMES,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.server.agent_definition import AgentDefinitionRevision

ORGANIZER_NAME = "EpisodeOrganizerAgent"
ORGANIZER_SCHEMA_VERSION = "episode-organizer-v1"


def organizer_definition(
    work_type: TmdbWorkType,
) -> AgentDefinitionRevision:
    return AgentDefinitionRevision.create(
        name=ORGANIZER_NAME,
        instructions=(
            f"{EPISODE_ORGANIZER_INSTRUCTIONS}\n"
            f"This run's authorized work_type is {work_type.value}."
        ),
        tools=EPISODE_ORGANIZER_TOOL_NAMES,
        schema_version=ORGANIZER_SCHEMA_VERSION,
    )
