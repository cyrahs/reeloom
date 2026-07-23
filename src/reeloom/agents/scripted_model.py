from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import TypeAlias

from agents import Model, ModelResponse
from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import TResponseInputItem, TResponseStreamEvent
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.tool import Tool
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam


@dataclass(frozen=True, slots=True)
class ToolCallStep:
    name: str
    arguments: Mapping[str, object] | str
    call_id: str
    expect_input_contains: str | None = None


@dataclass(frozen=True, slots=True)
class FinalStep:
    text: str
    expect_input_contains: str | None = None


ScriptStep: TypeAlias = ToolCallStep | FinalStep


class ScriptedModel(Model):
    """An offline SDK model whose responses are fixed by a test transcript."""

    def __init__(self, steps: tuple[ScriptStep, ...]) -> None:
        if not steps:
            raise ValueError("script must contain at least one step")
        self._steps = steps
        self._cursor = 0

    @property
    def consumed_steps(self) -> int:
        return self._cursor

    @property
    def exhausted(self) -> bool:
        return self._cursor == len(self._steps)

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        del (
            system_instructions,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id,
            conversation_id,
            prompt,
        )
        if self._cursor >= len(self._steps):
            raise AssertionError("scripted model received an unexpected turn")

        step = self._steps[self._cursor]
        self._cursor += 1
        expected = step.expect_input_contains
        if expected is not None and expected not in str(input):
            raise AssertionError(
                f"model input did not contain expected observation: {expected}"
            )

        if isinstance(step, ToolCallStep):
            arguments = (
                step.arguments
                if isinstance(step.arguments, str)
                else json.dumps(
                    step.arguments,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            output = [
                ResponseFunctionToolCall(
                    arguments=arguments,
                    call_id=step.call_id,
                    name=step.name,
                    type="function_call",
                )
            ]
        else:
            output = [
                ResponseOutputMessage(
                    id=f"message-{self._cursor}",
                    content=[
                        ResponseOutputText(
                            annotations=[],
                            text=step.text,
                            type="output_text",
                        )
                    ],
                    role="assistant",
                    status="completed",
                    type="message",
                )
            ]

        return ModelResponse(
            output=output,
            usage=Usage(
                requests=1,
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
            ),
            response_id=f"script-response-{self._cursor}",
        )

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        del (
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id,
            conversation_id,
            prompt,
        )

        async def unsupported() -> AsyncIterator[TResponseStreamEvent]:
            raise NotImplementedError("ScriptedModel only supports Runner.run")
            yield

        return unsupported()
