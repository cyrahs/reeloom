from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True, slots=True)
class AgentDefinitionRevision:
    definition_hash: str
    name: str
    instructions: str
    tools: tuple[str, ...]
    schema_version: str

    @classmethod
    def create(
        cls,
        *,
        name: str,
        instructions: str,
        tools: tuple[str, ...],
        schema_version: str,
    ) -> AgentDefinitionRevision:
        if (
            _NAME.fullmatch(name) is None
            or not isinstance(instructions, str)
            or not instructions
            or len(instructions.encode("utf-8")) > 64 * 1024
            or not isinstance(tools, tuple)
            or not tools
            or len(tools) > 64
            or any(_NAME.fullmatch(item) is None for item in tools)
            or len(set(tools)) != len(tools)
            or _NAME.fullmatch(schema_version) is None
        ):
            raise ValueError("invalid agent definition")
        payload = json.dumps(
            {
                "instructions": instructions,
                "name": name,
                "schema_version": schema_version,
                "tools": list(tools),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return cls(
            definition_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            name=name,
            instructions=instructions,
            tools=tools,
            schema_version=schema_version,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "definition_hash": self.definition_hash,
                "instructions": self.instructions,
                "name": self.name,
                "schema_version": self.schema_version,
                "tools": list(self.tools),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_value(cls, value: object) -> AgentDefinitionRevision:
        if not isinstance(value, dict) or set(value) != {
            "definition_hash",
            "instructions",
            "name",
            "schema_version",
            "tools",
        }:
            raise ValueError("invalid agent definition")
        raw_tools = value["tools"]
        if not isinstance(raw_tools, list):
            raise ValueError("invalid agent definition")
        definition = cls.create(
            name=value["name"],
            instructions=value["instructions"],
            tools=tuple(raw_tools),
            schema_version=value["schema_version"],
        )
        if value["definition_hash"] != definition.definition_hash:
            raise ValueError("invalid agent definition")
        return definition
