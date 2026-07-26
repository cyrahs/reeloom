from __future__ import annotations

import asyncio
import json

from reeloom.server.agent_definition import AgentDefinitionRevision
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
