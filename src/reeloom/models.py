"""Domain models.

Everything here is a frozen dataclass with an explicit JSON encoding. The
JSON forms are what land in the ``run`` row (``snapshot``, ``plan``,
``executed_moves``, ``result``); they are the only persisted representation,
so they are kept small and obvious.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self


class ReeloomError(Exception):
    """Base error carrying a stable code and actionable context."""

    def __init__(self, code: str, **context: Any) -> None:
        super().__init__(code if not context else f"{code}: {context}")
        self.code = code
        self.context = context


class PlanError(ReeloomError):
    """Raised when an Agent-submitted mapping cannot be compiled."""


class MediaType(StrEnum):
    ANIME = "anime"
    TV = "tv"
    MOVIE = "movie"

    @property
    def is_series(self) -> bool:
        return self in (MediaType.ANIME, MediaType.TV)


class FileKind(StrEnum):
    VIDEO = "video"
    SUBTITLE = "subtitle"
    OTHER = "other"


class SubtitleVariant(StrEnum):
    CHS = "chs"
    CHT = "cht"
    CHI = "chi"


class Root(StrEnum):
    """Which configured root a path is relative to."""

    INBOUND = "inbound"
    LIBRARY = "library"


class MoveKind(StrEnum):
    MEDIA = "media"
    """A mapped video or subtitle going into the library."""
    ACQUIRED_SUBTITLE = "acquired_subtitle"
    """A subtitle fetched from ACG.RIP and published into the library."""
    FOLDER_RENAME = "folder_rename"
    """An existing library folder renamed to add a missing ``{tmdb-id}``."""
    ARCHIVE = "archive"
    """Residual content moved to the watch root's ``archive`` bucket."""
    FAIL = "fail"
    """A duplicate or unusable file moved to the watch root's ``fail`` bucket."""


class MoveOutcome(StrEnum):
    MOVED = "moved"
    """The rename completed; this is the only outcome a revert undoes."""
    ALREADY_DONE = "already_done"
    """Source gone, destination present: a previous attempt finished it."""
    DUPLICATE = "duplicate"
    """Destination already existed; the source went to ``fail`` instead."""
    MISSING = "missing"
    """Neither source nor destination present; recorded and skipped."""


class RunState(StrEnum):
    PENDING = "pending"
    IDENTIFYING = "identifying"
    EXECUTING = "executing"
    ACQUIRING_SUBS = "acquiring_subs"
    REVERTING = "reverting"
    DONE = "done"
    NEEDS_ATTENTION = "needs_attention"
    DISCARDED = "discarded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        """States the worker drives forward without user input."""

        return self in _ACTIVE_STATES


_TERMINAL_STATES = frozenset(
    {RunState.DONE, RunState.DISCARDED, RunState.FAILED}
)
_ACTIVE_STATES = frozenset(
    {
        RunState.PENDING,
        RunState.IDENTIFYING,
        RunState.EXECUTING,
        RunState.ACQUIRING_SUBS,
        RunState.REVERTING,
    }
)


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    """One file observed in the intake folder before anything moved.

    ``candidate_id`` is the only handle the Agent ever gets; it never sees or
    supplies a filesystem path it can act on.
    """

    candidate_id: str
    relative_path: str
    kind: FileKind
    size_bytes: int
    variant: SubtitleVariant | None = None
    """Classified at scan time for subtitles; never an Agent decision."""

    def to_json(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "relative_path": self.relative_path,
            "kind": self.kind.value,
            "size_bytes": self.size_bytes,
            "variant": self.variant.value if self.variant else None,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        variant = payload.get("variant")
        return cls(
            candidate_id=payload["candidate_id"],
            relative_path=payload["relative_path"],
            kind=FileKind(payload["kind"]),
            size_bytes=payload["size_bytes"],
            variant=SubtitleVariant(variant) if variant else None,
        )


@dataclass(frozen=True, slots=True)
class EpisodeSpan:
    season: int
    episode_start: int
    episode_end: int

    def __post_init__(self) -> None:
        if self.season < 0 or self.season > 999:
            raise PlanError("invalid_season", season=self.season)
        if not 0 <= self.episode_start <= self.episode_end <= 9999:
            raise PlanError(
                "invalid_episode_span",
                start=self.episode_start,
                end=self.episode_end,
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "season": self.season,
            "episode_start": self.episode_start,
            "episode_end": self.episode_end,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        return cls(
            season=payload["season"],
            episode_start=payload["episode_start"],
            episode_end=payload["episode_end"],
        )


@dataclass(frozen=True, slots=True)
class MediaIdentity:
    """TMDB-backed identity of the whole intake folder.

    Movies use ``season``-less naming; the distinction is carried by
    ``media_type`` rather than by a separate model.
    """

    media_type: MediaType
    tmdb_id: int
    title: str
    year: int

    def to_json(self) -> dict[str, Any]:
        return {
            "media_type": self.media_type.value,
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "year": self.year,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        return cls(
            media_type=MediaType(payload["media_type"]),
            tmdb_id=payload["tmdb_id"],
            title=payload["title"],
            year=payload["year"],
        )


@dataclass(frozen=True, slots=True)
class Move:
    """One rename, always expressed as root-relative paths.

    Reverting a move is exactly swapping source and destination, which is why
    both sides carry their root.
    """

    kind: MoveKind
    source_root: Root
    source_path: str
    dest_root: Root
    dest_path: str
    candidate_id: str | None = None

    def reversed(self) -> Move:
        return Move(
            kind=self.kind,
            source_root=self.dest_root,
            source_path=self.dest_path,
            dest_root=self.source_root,
            dest_path=self.source_path,
            candidate_id=self.candidate_id,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "source_root": self.source_root.value,
            "source_path": self.source_path,
            "dest_root": self.dest_root.value,
            "dest_path": self.dest_path,
            "candidate_id": self.candidate_id,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        return cls(
            kind=MoveKind(payload["kind"]),
            source_root=Root(payload["source_root"]),
            source_path=payload["source_path"],
            dest_root=Root(payload["dest_root"]),
            dest_path=payload["dest_path"],
            candidate_id=payload.get("candidate_id"),
        )


@dataclass(frozen=True, slots=True)
class ExecutedMove:
    """What the executor actually did.

    This is the sole basis for crash replay and for reapply's revert step, so
    it records the move as performed — a duplicate records the move into
    ``fail``, not the library move that was planned.
    """

    move: Move
    outcome: MoveOutcome

    def to_json(self) -> dict[str, Any]:
        return {"move": self.move.to_json(), "outcome": self.outcome.value}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        return cls(
            move=Move.from_json(payload["move"]),
            outcome=MoveOutcome(payload["outcome"]),
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """The complete set of intended renames for one run."""

    identity: MediaIdentity
    moves: tuple[Move, ...]
    unmapped: tuple[str, ...] = ()
    """Candidate IDs the Agent deliberately left out; they go to ``archive``."""
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_json(),
            "moves": [move.to_json() for move in self.moves],
            "unmapped": list(self.unmapped),
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        return cls(
            identity=MediaIdentity.from_json(payload["identity"]),
            moves=tuple(Move.from_json(item) for item in payload["moves"]),
            unmapped=tuple(payload.get("unmapped", ())),
            notes=payload.get("notes", ""),
        )


@dataclass(frozen=True, slots=True)
class RunResult:
    """User-visible summary of a settled run."""

    moved: int = 0
    duplicates: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    archived: int = 0
    subtitles_acquired: int = 0
    subtitle_note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "moved": self.moved,
            "duplicates": list(self.duplicates),
            "missing": list(self.missing),
            "archived": self.archived,
            "subtitles_acquired": self.subtitles_acquired,
            "subtitle_note": self.subtitle_note,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        return cls(
            moved=payload.get("moved", 0),
            duplicates=tuple(payload.get("duplicates", ())),
            missing=tuple(payload.get("missing", ())),
            archived=payload.get("archived", 0),
            subtitles_acquired=payload.get("subtitles_acquired", 0),
            subtitle_note=payload.get("subtitle_note", ""),
        )


@dataclass(frozen=True, slots=True)
class WatchConfig:
    id: str
    name: str
    inbound_root: str
    library_root: str
    media_type: MediaType
    enabled: bool = True
    stability_seconds: int = 120
    acquire_subtitles: bool = False
    notify: bool = True


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    config_id: str
    folder_name: str
    state: RunState
    snapshot: tuple[SnapshotFile, ...] = ()
    plan: Plan | None = None
    executed_moves: tuple[ExecutedMove, ...] = ()
    result: RunResult | None = None
    error: dict[str, Any] | None = None
    attempts: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def candidate(self, candidate_id: str) -> SnapshotFile:
        for item in self.snapshot:
            if item.candidate_id == candidate_id:
                return item
        raise PlanError("unknown_candidate", candidate_id=candidate_id)
