"""The tool-call loop.

A plain sequential loop: ask the model, run the one tool it asked for, feed
the observation back. It ends only when a terminal tool raises ``Finished`` —
assistant prose can never settle a run, it can only escalate to a human.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from reeloom.adapters.llm import Conversation, Model, ModelError
from reeloom.models import ReeloomError

_LOGGER = logging.getLogger(__name__)

MAX_TURNS = 24
MAX_CONSECUTIVE_TEXT = 2


class Finished(Exception):
    """Raised by a terminal tool to end the loop with a result."""

    def __init__(self, result: Any) -> None:
        super().__init__("finished")
        self.result = result


class Escalate(Exception):
    """Raised when only a human can move the run forward."""

    def __init__(self, code: str, **context: Any) -> None:
        super().__init__(code)
        self.code = code
        self.context = context


class ToolBox(Protocol):
    def schemas(self) -> list[dict[str, Any]]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> Any: ...


async def run_loop(
    model: Model,
    conversation: Conversation,
    toolbox: ToolBox,
    *,
    max_turns: int = MAX_TURNS,
) -> Any:
    schemas = toolbox.schemas()
    text_streak = 0

    for turn in range(max_turns):
        reply = await model.complete(conversation, schemas)
        conversation.assistant(reply)

        if not reply.tool_calls:
            text_streak += 1
            if text_streak >= MAX_CONSECUTIVE_TEXT:
                raise Escalate("agent_stopped", message=reply.content[:1000])
            conversation.user(
                "Continue by calling a tool. If you cannot decide, call"
                " report_problem with what is blocking you."
            )
            continue

        text_streak = 0
        for call in reply.tool_calls:
            try:
                arguments = call.parsed()
            except ModelError as error:
                conversation.tool_result(call, {"error": error.code})
                continue
            try:
                observation = await toolbox.call(call.name, arguments)
            except Finished as done:
                return done.result
            except Escalate:
                raise
            except ReeloomError as error:
                _LOGGER.debug("tool %s rejected: %s", call.name, error.code)
                observation = {"error": error.code, **error.context}
            except Exception as error:  # noqa: BLE001 - reported to the model
                _LOGGER.warning("tool %s failed: %s", call.name, error)
                observation = {"error": "tool_failed", "detail": str(error)[:200]}
            conversation.tool_result(call, observation)

    raise Escalate("max_turns_exhausted", turns=max_turns)
