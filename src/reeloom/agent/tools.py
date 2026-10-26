"""The Agent's entire capability surface.

Read-only TMDB lookups, two terminal verdicts — ``submit_plan`` and
``split_folder`` — and an escalation hatch. No filesystem access, no shell, no
URLs, no apply. Every argument is validated before it reaches an adapter, and
both verdicts take candidate IDs (plus episode numbers) only — never a path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from reeloom.adapters.tmdb import MAX_SEARCH_PAGE, TmdbClient
from reeloom.agent.loop import Escalate, Finished
from reeloom.library import find_existing_folder
from reeloom.models import (
    EpisodeSpan,
    FileKind,
    FolderSplit,
    MediaIdentity,
    MediaType,
    PlanError,
    Run,
    WatchConfig,
)
from reeloom.planner import MappingEntry, compile_plan

_LOGGER = logging.getLogger(__name__)

MAX_ENTRIES = 500
MAX_SPLIT_GROUPS = 8


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
                "Search TMDB by title. Returns one page of up to 20 candidates"
                " with `page` and `total_pages`; when the title is not on this"
                " page, call again with the next page. Adult titles are"
                " included and flagged with `adult`.",
                {
                    "query": {"type": "string", "maxLength": 200},
                    "page": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_PAGE,
                    },
                },
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
                    "notes": {
                        "type": "string",
                        "maxLength": 500,
                        "description": (
                            "Shown to the user; write in Simplified Chinese"
                            " (简体中文)."
                        ),
                    },
                },
                ["tmdb_id", "entries"],
            ),
            _tool(
                "split_folder",
                "Split a folder that holds more than one distinct title."
                " Give one group of candidate IDs per title. The first group"
                " stays in this folder and is identified again right after;"
                " every further group moves to a new folder of its own and is"
                " identified as a separate run. Candidates you do not list"
                " stay with the first group. Every group needs at least one"
                " video; put subtitles in the group of their video.",
                {
                    "groups": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": MAX_SPLIT_GROUPS,
                        "items": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_ENTRIES,
                            "items": {"type": "string"},
                        },
                    },
                    "reason": {
                        "type": "string",
                        "maxLength": 500,
                        "description": (
                            "Shown to the user; write in Simplified Chinese"
                            " (简体中文)."
                        ),
                    },
                },
                ["groups"],
            ),
            _tool(
                "report_problem",
                "Escalate to a human when the folder cannot be identified.",
                {
                    "reason": {
                        "type": "string",
                        "maxLength": 500,
                        "description": (
                            "Shown to the user; write in Simplified Chinese"
                            " (简体中文)."
                        ),
                    }
                },
                ["reason"],
            ),
        ]
        return schemas

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        match name:
            case "search_tmdb":
                page = (
                    1
                    if arguments.get("page") is None
                    else _integer(arguments, "page")
                )
                if not 1 <= page <= MAX_SEARCH_PAGE:
                    raise PlanError(
                        "invalid_argument",
                        argument="page",
                        expected=f"1..{MAX_SEARCH_PAGE}",
                    )
                found = await self._tmdb.search(
                    _text(arguments, "query"), movie=self._movie, page=page
                )
                return found.to_json()
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
            case "split_folder":
                return self._split(arguments)
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
            original_title=str(details.get("original_title") or ""),
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

    def _split(self, arguments: dict[str, Any]) -> Any:
        if self._run.executed_moves:
            # Once files have moved into the library the intake folder no
            # longer describes the run, so there is nothing left to split.
            raise PlanError("split_unavailable_after_execution")
        raw_groups = arguments.get("groups")
        if not isinstance(raw_groups, list):
            raise PlanError("groups_must_be_a_list")
        if not 2 <= len(raw_groups) <= MAX_SPLIT_GROUPS:
            raise PlanError(
                "invalid_group_count",
                count=len(raw_groups),
                expected=f"2..{MAX_SPLIT_GROUPS}",
            )

        seen: set[str] = set()
        groups: list[tuple[str, ...]] = []
        for number, raw in enumerate(raw_groups, start=1):
            if not isinstance(raw, list) or not raw:
                raise PlanError("group_must_be_a_non_empty_list", group=number)
            paths: list[str] = []
            has_video = False
            for candidate_id in raw:
                if not isinstance(candidate_id, str):
                    raise PlanError("invalid_candidate", group=number)
                if candidate_id in seen:
                    raise PlanError("duplicate_candidate", candidate_id=candidate_id)
                seen.add(candidate_id)
                item = self._run.candidate(candidate_id)
                has_video |= item.kind is FileKind.VIDEO
                paths.append(item.relative_path)
            if not has_video:
                raise PlanError("group_without_video", group=number)
            groups.append(tuple(paths))

        # Paths come from the snapshot; the first group is whatever stays.
        raise Finished(
            FolderSplit(
                groups=tuple(groups[1:]),
                reason=str(arguments.get("reason") or "")[:500],
            )
        )


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
