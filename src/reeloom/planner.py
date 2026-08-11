"""Mapping validation and plan compilation.

This is the boundary the Agent submits across. It hands over candidate IDs and
episode numbers; everything spatial — which root, which folder, which
filename, whether two files would land on top of each other — is decided here
and rejected here. Rejections are structured so the model can correct itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from reeloom.models import (
    EpisodeSpan,
    FileKind,
    MediaIdentity,
    Move,
    MoveKind,
    Plan,
    PlanError,
    Root,
    SnapshotFile,
)
from reeloom.naming import (
    ExistingFolder,
    episode_path,
    movie_path,
    resolve_library_folder,
)

MAX_MAPPED_FILES = 2_000


@dataclass(frozen=True, slots=True)
class MappingEntry:
    """One Agent assertion: this candidate is this episode.

    ``span`` is required for series and must be absent for movies.
    """

    candidate_id: str
    span: EpisodeSpan | None = None


def compile_plan(
    identity: MediaIdentity,
    snapshot: tuple[SnapshotFile, ...],
    entries: tuple[MappingEntry, ...],
    *,
    folder_name: str,
    existing_folder: ExistingFolder | None = None,
    notes: str = "",
) -> Plan:
    """Turn a validated mapping into the immutable set of intended renames."""

    if not entries:
        raise PlanError("empty_mapping")
    if len(entries) > MAX_MAPPED_FILES:
        raise PlanError("too_many_mapped", count=len(entries))

    by_id = {item.candidate_id: item for item in snapshot}
    seen: set[str] = set()
    for entry in entries:
        if entry.candidate_id not in by_id:
            raise PlanError("unknown_candidate", candidate_id=entry.candidate_id)
        if entry.candidate_id in seen:
            raise PlanError("duplicate_candidate", candidate_id=entry.candidate_id)
        seen.add(entry.candidate_id)
        if by_id[entry.candidate_id].kind is FileKind.OTHER:
            raise PlanError(
                "unmappable_kind", candidate_id=entry.candidate_id
            )

    target_folder, rename_from = resolve_library_folder(identity, existing_folder)
    moves: list[Move] = []
    if rename_from is not None:
        moves.append(
            Move(
                kind=MoveKind.FOLDER_RENAME,
                source_root=Root.LIBRARY,
                source_path=rename_from,
                dest_root=Root.LIBRARY,
                dest_path=target_folder,
            )
        )

    if identity.media_type.is_series:
        media_moves = _series_moves(
            identity, by_id, entries, folder_name, target_folder
        )
    else:
        media_moves = _movie_moves(
            identity, by_id, entries, folder_name, target_folder
        )
    moves.extend(media_moves)

    destinations = [move.dest_path for move in media_moves]
    duplicates = {path for path in destinations if destinations.count(path) > 1}
    if duplicates:
        raise PlanError("destination_collision", destinations=sorted(duplicates))

    unmapped = tuple(
        item.candidate_id for item in snapshot if item.candidate_id not in seen
    )
    return Plan(
        identity=identity, moves=tuple(moves), unmapped=unmapped, notes=notes
    )


def _series_moves(
    identity: MediaIdentity,
    by_id: dict[str, SnapshotFile],
    entries: tuple[MappingEntry, ...],
    folder_name: str,
    target_folder: str,
) -> list[Move]:
    moves: list[Move] = []
    for entry in entries:
        if entry.span is None:
            raise PlanError("missing_episode", candidate_id=entry.candidate_id)
        candidate = by_id[entry.candidate_id]
        extension = PurePosixPath(candidate.relative_path).suffix
        if candidate.kind is FileKind.SUBTITLE:
            destination = episode_path(
                identity,
                entry.span,
                extension,
                variant=candidate.variant,
                root=target_folder,
            )
        else:
            destination = episode_path(
                identity, entry.span, extension, root=target_folder
            )
        moves.append(_move(candidate, folder_name, destination))
    return moves


def _movie_moves(
    identity: MediaIdentity,
    by_id: dict[str, SnapshotFile],
    entries: tuple[MappingEntry, ...],
    folder_name: str,
    target_folder: str,
) -> list[Move]:
    videos = [
        entry
        for entry in entries
        if by_id[entry.candidate_id].kind is FileKind.VIDEO
    ]
    if len(videos) != 1:
        raise PlanError("movie_needs_exactly_one_video", count=len(videos))

    moves: list[Move] = []
    for entry in entries:
        if entry.span is not None:
            raise PlanError(
                "movie_has_no_episodes", candidate_id=entry.candidate_id
            )
        candidate = by_id[entry.candidate_id]
        extension = PurePosixPath(candidate.relative_path).suffix
        destination = movie_path(
            identity,
            extension,
            variant=(
                candidate.variant
                if candidate.kind is FileKind.SUBTITLE
                else None
            ),
            root=target_folder,
        )
        moves.append(_move(candidate, folder_name, destination))
    return moves


def _move(
    candidate: SnapshotFile, folder_name: str, destination: PurePosixPath
) -> Move:
    return Move(
        kind=MoveKind.MEDIA,
        source_root=Root.INBOUND,
        source_path=f"{folder_name}/{candidate.relative_path}",
        dest_root=Root.LIBRARY,
        dest_path=destination.as_posix(),
        candidate_id=candidate.candidate_id,
    )
