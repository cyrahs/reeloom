from __future__ import annotations

import asyncio
import json

from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.server.agent_definition import AgentDefinitionRevision
from reeloom.server.organizer_definition import (
    ANIME_ORGANIZER_TOOL_NAMES,
    EPISODE_ORGANIZER_TOOL_NAMES,
    LEGACY_EPISODE_ORGANIZER_TOOL_NAMES,
    LEGACY_ORGANIZER_SCHEMA_VERSION,
    MOVIE_ORGANIZER_SCHEMA_VERSION,
    M13_PROBE_ORGANIZER_TOOL_NAMES,
    ORGANIZER_NAME,
    ORGANIZER_SCHEMA_VERSION,
    PREVIOUS_ORGANIZER_SCHEMA_VERSION,
    V4_ORGANIZER_SCHEMA_VERSION,
    V5_ORGANIZER_SCHEMA_VERSION,
    V2_ORGANIZER_SCHEMA_VERSION,
    V3_ORGANIZER_SCHEMA_VERSION,
    is_supported_organizer_definition,
    organizer_definition,
)
from reeloom.server.session import InMemorySessionRepository, RepositoryAgentSession


def test_agent_definition_is_content_addressed() -> None:
    first = AgentDefinitionRevision.create(
        name="EpisodeOrganizerAgent",
        instructions="Use typed tools.",
        tools=("list_candidates", "submit_mapping"),
        schema_version="organizer-v1",
    )
    same = AgentDefinitionRevision.create(
        name="EpisodeOrganizerAgent",
        instructions="Use typed tools.",
        tools=("list_candidates", "submit_mapping"),
        schema_version="organizer-v1",
    )
    changed = AgentDefinitionRevision.create(
        name="EpisodeOrganizerAgent",
        instructions="Use typed tools carefully.",
        tools=("list_candidates", "submit_mapping"),
        schema_version="organizer-v1",
    )

    assert first == same
    assert AgentDefinitionRevision.from_value(
        json.loads(first.to_json())
    ) == first
    assert first.definition_hash.startswith("sha256:")
    assert changed.definition_hash != first.definition_hash


def test_v7_keeps_m13_capability_explicit_and_history_readable() -> None:
    current = organizer_definition(TmdbWorkType.ANIME)

    assert current.schema_version == ORGANIZER_SCHEMA_VERSION
    assert current.schema_version == "episode-organizer-v7"
    assert current.tools == ANIME_ORGANIZER_TOOL_NAMES
    assert "check_sub_from_video" in current.tools
    assert "search_sub" in current.tools
    assert "select_subtitle_release" in current.tools
    disabled = organizer_definition(
        TmdbWorkType.ANIME,
        subtitle_acquisition_enabled=False,
    )
    assert disabled.schema_version == V4_ORGANIZER_SCHEMA_VERSION
    assert disabled.tools == EPISODE_ORGANIZER_TOOL_NAMES
    assert "check_sub_from_video" not in disabled.instructions
    television = organizer_definition(TmdbWorkType.TV_SERIES)
    assert television.schema_version == "episode-organizer-v4"
    assert television.tools == EPISODE_ORGANIZER_TOOL_NAMES
    assert "check_sub_from_video" not in television.tools
    assert (
        organizer_definition(TmdbWorkType.MOVIE).schema_version
        == MOVIE_ORGANIZER_SCHEMA_VERSION
        == "movie-organizer-v4"
    )
    for version, tools, allow_v1 in (
        (
            LEGACY_ORGANIZER_SCHEMA_VERSION,
            LEGACY_EPISODE_ORGANIZER_TOOL_NAMES,
            True,
        ),
        (V2_ORGANIZER_SCHEMA_VERSION, EPISODE_ORGANIZER_TOOL_NAMES, False),
        (V3_ORGANIZER_SCHEMA_VERSION, EPISODE_ORGANIZER_TOOL_NAMES, False),
        (V4_ORGANIZER_SCHEMA_VERSION, EPISODE_ORGANIZER_TOOL_NAMES, False),
        (
            V5_ORGANIZER_SCHEMA_VERSION,
            M13_PROBE_ORGANIZER_TOOL_NAMES,
            False,
        ),
        (
            PREVIOUS_ORGANIZER_SCHEMA_VERSION,
            ANIME_ORGANIZER_TOOL_NAMES,
            False,
        ),
    ):
        historical = AgentDefinitionRevision.create(
            name=ORGANIZER_NAME,
            instructions="Historical organizer.",
            tools=tools,
            schema_version=version,
        )
        assert is_supported_organizer_definition(
            historical,
            TmdbWorkType.ANIME,
            allow_v1=allow_v1,
        )


def test_repository_session_has_cas_batches_and_restart_projection() -> None:
    async def scenario() -> None:
        repository = InMemorySessionRepository()
        first = RepositoryAgentSession(
            repository=repository,
            run_id="run-1",
            session_id="run-1",
        )
        await first.add_items(
            [{"role": "user", "content": "untrusted filename"}]
        )
        await first.add_items(
            [{"role": "assistant", "content": "bounded reply"}]
        )

        recovered = RepositoryAgentSession(
            repository=repository,
            run_id="run-1",
            session_id="run-1",
        )

        assert await recovered.get_items() == [
            {"role": "user", "content": "untrusted filename"},
            {"role": "assistant", "content": "bounded reply"},
        ]
        assert repository.batch_count == 2
        assert repository.revision == 2

    asyncio.run(scenario())
