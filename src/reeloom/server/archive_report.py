from __future__ import annotations

import json
from collections.abc import Iterable

from reeloom.kernel.archive_directory import (
    ArchiveDirectoryCapability,
    ArchiveDirectoryListing,
    ArchiveSearchRecord,
)
from reeloom.runtime.state import RunState
from reeloom.runtime.state_codec import (
    _archive_capability,
    _archive_listing,
    _archive_search,
)


def archive_report_from_state(
    state: RunState,
) -> dict[str, object] | None:
    return _project(
        state.archive_directory_capabilities,
        state.archive_searches,
        state.archive_directory_listings,
    )


def archive_report_from_projection(
    value: object,
) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    raw_capabilities = value.get("archive_directory_capabilities")
    raw_searches = value.get("archive_searches")
    raw_listings = value.get("archive_directory_listings")
    if not all(
        isinstance(item, list)
        for item in (raw_capabilities, raw_searches, raw_listings)
    ):
        return None
    try:
        return _project(
            tuple(_archive_capability(item) for item in raw_capabilities),
            tuple(_archive_search(item) for item in raw_searches),
            tuple(_archive_listing(item) for item in raw_listings),
        )
    except (TypeError, ValueError):
        return None


def _project(
    capabilities: Iterable[ArchiveDirectoryCapability],
    searches: Iterable[ArchiveSearchRecord],
    listings: Iterable[ArchiveDirectoryListing],
) -> dict[str, object] | None:
    caps = {item.directory_id: item for item in capabilities}
    search_items = tuple(searches)
    listing_items = tuple(listings)
    if not search_items:
        return None
    search_complete = _completed_searches(search_items)
    completed = _completed_listings(listing_items)
    matched_ids = {
        directory_id
        for search in search_items
        for directory_id in search.directory_ids
    }
    included = set(matched_ids)
    changed = True
    while changed:
        changed = False
        for capability in caps.values():
            if (
                capability.parent_id in included
                and capability.directory_id not in included
            ):
                included.add(capability.directory_id)
                changed = True
    children: dict[str, list[ArchiveDirectoryCapability]] = {}
    for item in caps.values():
        if item.directory_id in included and item.parent_id in included:
            children.setdefault(str(item.parent_id), []).append(item)
    videos: dict[str, set[str]] = {}
    for listing in listing_items:
        if listing.directory_id in included:
            videos.setdefault(listing.directory_id, set()).update(
                listing.videos
            )
    entries: list[dict[str, object]] = []
    visited: set[str] = set()

    def append_directory(
        item: ArchiveDirectoryCapability,
        parent_entry_id: int | None,
    ) -> None:
        if item.directory_id in visited:
            return
        visited.add(item.directory_id)
        entry_id = len(entries) + 1
        entries.append(
            {
                "entry_id": entry_id,
                "parent_entry_id": parent_entry_id,
                "kind": "directory",
                "name": item.name,
                "depth": item.depth,
                "listed": item.directory_id in completed,
            }
        )
        nested: list[
            tuple[str, str, str | ArchiveDirectoryCapability]
        ] = [
            (name.casefold(), name, name)
            for name in videos.get(item.directory_id, set())
        ]
        nested.extend(
            (
                child.name.casefold(),
                child.name,
                child,
            )
            for child in children.get(item.directory_id, [])
        )
        for _, _, value in sorted(
            nested,
            key=lambda nested_item: nested_item[:2],
        ):
            if isinstance(value, ArchiveDirectoryCapability):
                append_directory(value, entry_id)
                continue
            entries.append(
                {
                    "entry_id": len(entries) + 1,
                    "parent_entry_id": entry_id,
                    "kind": "video",
                    "name": value,
                    "depth": item.depth + 1,
                    "listed": True,
                }
            )

    roots = sorted(
        (
            caps[item]
            for item in matched_ids
            if item in caps
        ),
        key=lambda item: (
            item.name.casefold(),
            item.name,
            item.directory_id,
        ),
    )
    for root in roots:
        append_directory(root, None)
    truncated = len(entries) > 200
    entries = entries[:200]
    complete = (
        search_complete
        and all(item in completed for item in included)
        and not truncated
    )
    observed_at = max(
        [
            item.observed_at for item in search_items
        ]
        + [item.observed_at for item in listing_items]
    )
    latest = search_items[-1]
    report = {
        "status": "checked" if complete else "incomplete",
        "work_type": (
            "tv"
            if latest.work_type.value == "tv_series"
            else latest.work_type.value
        ),
        "tmdb_id": latest.tmdb_id,
        "searches": [
            {
                "mode": item.mode,
                "match_count": len(item.directory_ids),
                "complete": item.complete,
            }
            for item in search_items
        ],
        "entries": entries,
        "possible_existing_archive": bool(matched_ids),
        "advisory_only": True,
        "observed_at": observed_at.isoformat(),
    }
    while (
        len(
            json.dumps(
                report,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        > 64 * 1024
        and entries
    ):
        entries.pop()
        report["status"] = "incomplete"
    return report


def _completed_searches(
    searches: tuple[ArchiveSearchRecord, ...],
) -> bool:
    expected: dict[tuple[object, ...], int | None] = {}
    completed: set[tuple[object, ...]] = set()
    keys: set[tuple[object, ...]] = set()
    for item in searches:
        key = (item.work_type, item.tmdb_id, item.mode, item.query)
        keys.add(key)
        if item.cursor == 0:
            completed.discard(key)
        elif expected.get(key) != item.cursor:
            completed.discard(key)
            expected[key] = None
            continue
        expected[key] = item.next_cursor
        if item.next_cursor is None and item.complete:
            completed.add(key)
    return bool(keys) and keys <= completed


def _completed_listings(
    listings: tuple[ArchiveDirectoryListing, ...],
) -> set[str]:
    expected: dict[str, int | None] = {}
    completed: set[str] = set()
    for item in listings:
        key = item.directory_id
        if item.cursor == 0:
            completed.discard(key)
        elif expected.get(key) != item.cursor:
            completed.discard(key)
            expected[key] = None
            continue
        expected[key] = item.next_cursor
        if item.next_cursor is None and item.complete:
            completed.add(key)
    return completed
