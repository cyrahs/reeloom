"""The Agent's entire capability surface.

Five tools: four read-only TMDB lookups and one terminal submission, plus an
escalation hatch. No filesystem access, no shell, no URLs, no apply. Every
argument is validated before it reaches an adapter, and ``submit_plan`` takes
candidate IDs and episode numbers only — never a path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from reeloom.adapters.tmdb import TmdbClient
from reeloom.agent.loop import Escalate, Finished
from reeloom.library import find_existing_folder
from reeloom.models import (
    EpisodeSpan,
    MediaIdentity,
    MediaType,
    PlanError,
    Run,
    WatchConfig,
)
from reeloom.planner import MappingEntry, compile_plan

_LOGGER = logging.getLogger(__name__)

MAX_ENTRIES = 500


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


class IdentificationTools:
    def __init__(
        self,
        run: Run,
        config: WatchConfig,
        tmdb: TmdbClient,
    ) -> None:
        self._run = run
        self._config = config
        self._tmdb = tmdb
        self._movie = config.media_type is MediaType.MOVIE
        self.rejections = 0

    def schemas(self) -> list[dict[str, Any]]:
        entry_properties: dict[str, Any] = {
            "candidate_id": {"type": "string"},
        }
        required = ["candidate_id"]
        if not self._movie:
            entry_properties |= {
                "season": {"type": "integer", "minimum": 0},
                "episode_start": {"type": "integer", "minimum": 0},
                "episode_end": {"type": "integer", "minimum": 0},
            }
            required += ["season", "episode_start", "episode_end"]

        lookup = (
            _tool(
                "get_movie",
                "Fetch details for a movie by TMDB id.",
                {"tmdb_id": {"type": "integer"}},
                ["tmdb_id"],
            )
            if self._movie
            else _tool(
                "get_series",
                "Fetch a series and its season list by TMDB id.",
                {"tmdb_id": {"type": "integer"}},
                ["tmdb_id"],
            )
        )

        schemas = [
            _tool(
                "search_tmdb",
                "Search TMDB by title. Returns at most 8 candidates.",
                {"query": {"type": "string", "maxLength": 200}},
                ["query"],
            ),
            lookup,
        ]
        if not self._movie:
            schemas.append(
                _tool(
                    "get_season",
                    "List the episodes TMDB has for one season of a series.",
                    {
                        "tmdb_id": {"type": "integer"},
                        "season": {"type": "integer", "minimum": 0},
                    },
                    ["tmdb_id", "season"],
                )
            )
        schemas += [
            _tool(
                "submit_plan",
                "Submit the final mapping. Call once, when you are confident.",
                {
                    "tmdb_id": {"type": "integer"},
                    "entries": {
                        "type": "array",
                        "maxItems": MAX_ENTRIES,
                        "items": {
                            "type": "object",
                            "properties": entry_properties,
                            "required": required,
                            "additionalProperties": False,
                        },
                    },
                    "notes": {"type": "string", "maxLength": 500},
                },
                ["tmdb_id", "entries"],
            ),
            _tool(
                "report_problem",
                "Escalate to a human when the folder cannot be identified.",
                {"reason": {"type": "string", "maxLength": 500}},
                ["reason"],
            ),
        ]
        return schemas

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        match name:
            case "search_tmdb":
                hits = await self._tmdb.search(
                    _text(arguments, "query"), movie=self._movie
                )
                return {"results": [hit.to_json() for hit in hits]}
            case "get_series":
                return await self._tmdb.get_series(_integer(arguments, "tmdb_id"))
            case "get_movie":
                return await self._tmdb.get_movie(_integer(arguments, "tmdb_id"))
            case "get_season":
                return await self._tmdb.get_season(
                    _integer(arguments, "tmdb_id"), _integer(arguments, "season")
                )
            case "submit_plan":
                return await self._submit(arguments)
            case "report_problem":
                raise Escalate(
                    "agent_reported_problem", reason=_text(arguments, "reason")
                )
            case _:
                raise PlanError("unknown_tool", tool=name)

    async def _submit(self, arguments: dict[str, Any]) -> Any:
        tmdb_id = _integer(arguments, "tmdb_id")
        raw_entries = arguments.get("entries")
        if not isinstance(raw_entries, list):
            raise PlanError("entries_must_be_a_list")
        if len(raw_entries) > MAX_ENTRIES:
            raise PlanError("too_many_entries", count=len(raw_entries))

        entries: list[MappingEntry] = []
        for item in raw_entries:
            if not isinstance(item, dict):
                raise PlanError("entry_must_be_an_object")
            candidate_id = _text(item, "candidate_id")
            if self._movie:
                entries.append(MappingEntry(candidate_id))
                continue
            entries.append(
                MappingEntry(
                    candidate_id,
                    EpisodeSpan(
                        season=_integer(item, "season"),
                        episode_start=_integer(item, "episode_start"),
                        episode_end=_integer(item, "episode_end"),
                    ),
                )
            )

        # Title and year come from TMDB, not from the model.
        if self._movie:
            details = await self._tmdb.get_movie(tmdb_id)
        else:
            details = await self._tmdb.get_series(tmdb_id)
        if not details.get("title") or details.get("year") is None:
            raise PlanError("tmdb_entry_incomplete", tmdb_id=tmdb_id)

        identity = MediaIdentity(
            media_type=self._config.media_type,
            tmdb_id=tmdb_id,
            title=details["title"],
            year=details["year"],
        )
        existing = find_existing_folder(
            Path(self._config.library_root), identity
        )
        try:
            plan = compile_plan(
                identity,
                self._run.snapshot,
                tuple(entries),
                folder_name=self._run.folder_name,
                existing_folder=existing,
                notes=str(arguments.get("notes") or "")[:500],
            )
        except PlanError:
            self.rejections += 1
            raise
        raise Finished(plan)


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PlanError("invalid_argument", argument=key, expected="text")
    return value.strip()


def _integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanError("invalid_argument", argument=key, expected="integer")
    return value
