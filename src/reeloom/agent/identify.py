"""Identification: run the Agent until it submits a compilable plan."""

from __future__ import annotations

import logging
from typing import Protocol

from reeloom.adapters.llm import Conversation, Model
from reeloom.adapters.tmdb import TmdbClient
from reeloom.agent.loop import Escalate, run_loop
from reeloom.agent.prompts import (
    SYSTEM,
    chat_message,
    revision_message,
    task_message,
)
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

    def __init__(self, clients: ClientFactory, database) -> None:
        self._clients = clients
        self._db = database

    async def identify(self, run: Run, config: WatchConfig) -> Plan:
        if len(run.snapshot) > MAX_MAPPABLE_FILES:
            raise NeedsAttention("folder_too_large", files=len(run.snapshot))

        conversation = Conversation()
        conversation.system(SYSTEM)
        conversation.user(task_message(run, config))
        # The run's whole chat log is the model's context: Q&A as background,
        # revision messages as binding instructions, in that order so the
        # instructions land last.
        history = await self._db.list_interactions(run.id)
        chat = [item for item in history if item["role"] != "revision"]
        if chat:
            conversation.user(chat_message(chat))
        for item in history:
            if item["role"] == "revision":
                conversation.user(revision_message(item["content"]))

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
