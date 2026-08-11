"""Identification: run the Agent until it submits a compilable plan."""

from __future__ import annotations

import logging
from typing import Protocol

from reeloom.adapters.llm import Conversation, Model
from reeloom.adapters.tmdb import TmdbClient
from reeloom.agent.loop import Escalate, run_loop
from reeloom.agent.prompts import SYSTEM, revision_message, task_message
from reeloom.agent.tools import IdentificationTools
from reeloom.models import Plan, Run, WatchConfig
from reeloom.server.worker import NeedsAttention

_LOGGER = logging.getLogger(__name__)

MAX_MAPPABLE_FILES = 500


class ClientFactory(Protocol):
    async def model(self) -> Model: ...

    async def tmdb(self) -> TmdbClient: ...


class AgentIdentifier:
    """Implements the worker's ``Identifier`` protocol."""

    def __init__(self, clients: ClientFactory) -> None:
        self._clients = clients

    async def identify(self, run: Run, config: WatchConfig) -> Plan:
        if len(run.snapshot) > MAX_MAPPABLE_FILES:
            raise NeedsAttention("folder_too_large", files=len(run.snapshot))

        conversation = Conversation()
        conversation.system(SYSTEM)
        conversation.user(task_message(run, config))
        for note in run.extra.get("revisions", []):
            conversation.user(revision_message(note))

        model = await self._clients.model()
        tmdb = await self._clients.tmdb()
        tools = IdentificationTools(run, config, tmdb)

        try:
            plan = await run_loop(model, conversation, tools)
        except Escalate as error:
            raise NeedsAttention(error.code, **error.context) from error

        assert isinstance(plan, Plan)
        _LOGGER.info(
            "identified run=%s tmdb=%s moves=%s",
            run.id,
            plan.identity.tmdb_id,
            len(plan.moves),
        )
        return plan
