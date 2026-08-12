"""Version replacement decisions.

Pure functions, planner-style: the compare driver gathers what exists on disk
and what ffprobe says, and this module turns that into a deterministic
verdict per season and compiles the verdict into the plan as ordinary moves.
The model is never involved — anything that ends in a file leaving the
library is decided here, by code that a test can pin down.

The verdict is by season, not by episode: a BD batch is judged as a whole
against what the library (and any extra directories) already hold for that
season, so one undersized episode cannot block an otherwise clear upgrade.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, replace as dc_replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Mapping, Self, Sequence

from reeloom.adapters.ffprobe import Probe
from reeloom.models import (
    EpisodeSpan,
    Move,
    MoveKind,
    Plan,
    ReeloomError,
    Root,
    SnapshotFile,
)
from reeloom.naming import span_from_name
from reeloom.scanner import VIDEO_EXTENSIONS
from reeloom.trash import trash_relative


class ReplaceError(ReeloomError):
    pass


class QualitySignal(StrEnum):
    BETTER = "better"
    SAME = "same"
    WORSE = "worse"
    UNKNOWN = "unknown"


class GroupVerdict(StrEnum):
    IMPORT = "import"
    """Nothing existing overlaps; the plan runs as compiled."""
    REPLACE = "replace"
    """The incoming version wins; existing overlap goes to the trash area."""
    DISCARD = "discard"
    """The existing version wins; incoming overlap goes to the trash area."""
    MANUAL = "manual"
    """Signals conflict; a person decides."""


class ReplaceResolution(StrEnum):
    """What the user chose for a decision that was parked as MANUAL."""

    REPLACE = "replace"
    DISCARD_INCOMING = "discard_incoming"
    KEEP_BOTH = "keep_both"
    """Fall back to the pre-replacement behavior: collisions go to fail/."""


@dataclass(frozen=True, slots=True)
class ExistingFile:
    """One already-present video (or subtitle) found during comparison."""

    root: Root
    extra_base: str | None
    relative_path: str
    size_bytes: int
    span: EpisodeSpan | None

    @property
    def key(self) -> str:
        return f"{self.root.value}|{self.extra_base or ''}|{self.relative_path}"

    @property
    def is_video(self) -> bool:
        return PurePosixPath(self.relative_path).suffix.lower() in VIDEO_EXTENSIONS

    def to_json(self) -> dict[str, Any]:
        return {
            "root": self.root.value,
            "extra_base": self.extra_base,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "span": self.span.to_json() if self.span else None,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        span = payload.get("span")
        return cls(
            root=Root(payload["root"]),
            extra_base=payload.get("extra_base"),
            relative_path=payload["relative_path"],
            size_bytes=payload["size_bytes"],
            span=EpisodeSpan.from_json(span) if span else None,
        )


@dataclass(frozen=True, slots=True)
class EpisodeComparison:
    """One incoming video paired with what already exists for its episodes."""

    span: EpisodeSpan | None
    candidate_id: str
    incoming_bytes: int
    existing: tuple[ExistingFile, ...]
    existing_bytes: int
    """The largest existing instance — the one the ratio is judged against."""

    def to_json(self) -> dict[str, Any]:
        return {
            "span": self.span.to_json() if self.span else None,
            "candidate_id": self.candidate_id,
            "incoming_bytes": self.incoming_bytes,
            "existing": [item.to_json() for item in self.existing],
            "existing_bytes": self.existing_bytes,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        span = payload.get("span")
        return cls(
            span=EpisodeSpan.from_json(span) if span else None,
            candidate_id=payload["candidate_id"],
            incoming_bytes=payload["incoming_bytes"],
            existing=tuple(
                ExistingFile.from_json(item) for item in payload["existing"]
            ),
            existing_bytes=payload["existing_bytes"],
        )


@dataclass(frozen=True, slots=True)
class GroupDecision:
    """The verdict for one season (or the movie as a whole)."""

    season: int | None
    verdict: GroupVerdict
    ratio: float | None
    quality: QualitySignal
    overlap: tuple[EpisodeComparison, ...]
    new_episodes: tuple[int, ...]
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "season": self.season,
            "verdict": self.verdict.value,
            "ratio": self.ratio,
            "quality": self.quality.value,
            "overlap": [item.to_json() for item in self.overlap],
            "new_episodes": list(self.new_episodes),
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        return cls(
            season=payload.get("season"),
            verdict=GroupVerdict(payload["verdict"]),
            ratio=payload.get("ratio"),
            quality=QualitySignal(payload["quality"]),
            overlap=tuple(
                EpisodeComparison.from_json(item) for item in payload["overlap"]
            ),
            new_episodes=tuple(payload.get("new_episodes", ())),
            reason=payload.get("reason", ""),
        )


@dataclass(frozen=True, slots=True)
class ReplacementDecision:
    groups: tuple[GroupDecision, ...]
    existing_subtitles: tuple[str, ...] = ()
    """Basenames of subtitles already present, for the bundled-subtitle rule."""
    resolution: ReplaceResolution | None = None

    @property
    def needs_confirmation(self) -> bool:
        return any(group.verdict is GroupVerdict.MANUAL for group in self.groups)

    @property
    def changes_anything(self) -> bool:
        return any(
            group.verdict in (GroupVerdict.REPLACE, GroupVerdict.DISCARD)
            for group in self.groups
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "groups": [group.to_json() for group in self.groups],
            "existing_subtitles": list(self.existing_subtitles),
            "needs_confirmation": self.needs_confirmation,
            "resolution": self.resolution.value if self.resolution else None,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Self:
        resolution = payload.get("resolution")
        return cls(
            groups=tuple(
                GroupDecision.from_json(item) for item in payload["groups"]
            ),
            existing_subtitles=tuple(payload.get("existing_subtitles", ())),
            resolution=ReplaceResolution(resolution) if resolution else None,
        )


# ---- deciding -----------------------------------------------------------


def decide(
    plan: Plan,
    snapshot: Sequence[SnapshotFile],
    existing: Sequence[ExistingFile],
    *,
    probes_incoming: Mapping[str, Probe | None] | None = None,
    probes_existing: Mapping[str, Probe | None] | None = None,
    auto_ratio: float = 1.2,
) -> ReplacementDecision:
    """Judge the plan's videos against what already exists.

    ``existing`` is every file the driver found for this title across the
    library and the extra directories; only entries with a parseable span
    (or span-less entries for movies) ever pair up, so an unrecognized file
    can never be touched.
    """

    probes_incoming = probes_incoming or {}
    probes_existing = probes_existing or {}
    sizes = {item.candidate_id: item.size_bytes for item in snapshot}
    videos = [item for item in existing if item.is_video]
    is_series = plan.identity.media_type.is_series

    grouped: dict[int | None, list[tuple[Move, EpisodeSpan | None]]] = {}
    for move in plan.moves:
        if move.kind is not MoveKind.MEDIA:
            continue
        if PurePosixPath(move.dest_path).suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        span = span_from_name(PurePosixPath(move.dest_path).name)
        if is_series:
            if span is None:
                continue
            grouped.setdefault(span.season, []).append((move, span))
        else:
            grouped.setdefault(None, []).append((move, None))

    decisions = [
        _decide_group(
            season,
            moves,
            sizes,
            videos,
            probes_incoming,
            probes_existing,
            auto_ratio,
        )
        for season, moves in sorted(
            grouped.items(), key=lambda item: (item[0] is None, item[0])
        )
    ]
    return ReplacementDecision(
        groups=tuple(decisions),
        existing_subtitles=tuple(
            sorted(
                PurePosixPath(item.relative_path).name
                for item in existing
                if not item.is_video
            )
        ),
    )


def _decide_group(
    season: int | None,
    moves: list[tuple[Move, EpisodeSpan | None]],
    sizes: Mapping[str, int],
    videos: list[ExistingFile],
    probes_incoming: Mapping[str, Probe | None],
    probes_existing: Mapping[str, Probe | None],
    auto_ratio: float,
) -> GroupDecision:
    overlap: list[EpisodeComparison] = []
    new_episodes: list[int] = []
    for move, span in moves:
        matched = tuple(
            item for item in videos if _spans_pair(span, item.span)
        )
        candidate_id = move.candidate_id or ""
        if not matched:
            if span is not None:
                new_episodes.append(span.episode_start)
            continue
        overlap.append(
            EpisodeComparison(
                span=span,
                candidate_id=candidate_id,
                incoming_bytes=sizes.get(candidate_id, 0),
                existing=matched,
                existing_bytes=max(item.size_bytes for item in matched),
            )
        )

    if not overlap:
        return GroupDecision(
            season=season,
            verdict=GroupVerdict.IMPORT,
            ratio=None,
            quality=QualitySignal.UNKNOWN,
            overlap=(),
            new_episodes=tuple(new_episodes),
            reason="no_overlap",
        )

    if all(_has_identical_twin(item, moves) for item in overlap):
        return GroupDecision(
            season=season,
            verdict=GroupVerdict.DISCARD,
            ratio=1.0,
            quality=QualitySignal.SAME,
            overlap=tuple(overlap),
            new_episodes=tuple(new_episodes),
            reason="identical_files",
        )

    incoming_total = sum(item.incoming_bytes for item in overlap)
    existing_total = sum(item.existing_bytes for item in overlap)
    ratio = incoming_total / existing_total if existing_total else None
    quality = _quality_signal(overlap, probes_incoming, probes_existing)

    verdict, reason = _apply_matrix(ratio, quality, auto_ratio)
    return GroupDecision(
        season=season,
        verdict=verdict,
        ratio=round(ratio, 3) if ratio is not None else None,
        quality=quality,
        overlap=tuple(overlap),
        new_episodes=tuple(new_episodes),
        reason=reason,
    )


def _apply_matrix(
    ratio: float | None, quality: QualitySignal, auto_ratio: float
) -> tuple[GroupVerdict, str]:
    if ratio is None:
        return GroupVerdict.MANUAL, "existing_size_unknown"
    if quality is QualitySignal.WORSE:
        return GroupVerdict.MANUAL, "quality_conflict"
    if ratio >= auto_ratio:
        return GroupVerdict.REPLACE, "clear_upgrade"
    if ratio > 1.0:
        if quality is QualitySignal.BETTER:
            return GroupVerdict.REPLACE, "quality_upgrade"
        return GroupVerdict.MANUAL, "ambiguous_upgrade"
    if quality is QualitySignal.BETTER:
        return GroupVerdict.MANUAL, "smaller_but_better"
    return GroupVerdict.DISCARD, "not_an_upgrade"


def _spans_pair(incoming: EpisodeSpan | None, existing: EpisodeSpan | None) -> bool:
    if incoming is None or existing is None:
        # Movies: span-less videos in the same folder pair with each other.
        return incoming is None and existing is None
    return (
        incoming.season == existing.season
        and incoming.episode_start <= existing.episode_end
        and existing.episode_start <= incoming.episode_end
    )


def _has_identical_twin(
    item: EpisodeComparison, moves: list[tuple[Move, EpisodeSpan | None]]
) -> bool:
    """Same byte size and same extension: a re-drop of the same release."""

    extension = next(
        (
            PurePosixPath(move.dest_path).suffix.lower()
            for move, _ in moves
            if move.candidate_id == item.candidate_id
        ),
        None,
    )
    return any(
        existing.size_bytes == item.incoming_bytes
        and PurePosixPath(existing.relative_path).suffix.lower() == extension
        for existing in item.existing
    )


def _quality_signal(
    overlap: Sequence[EpisodeComparison],
    probes_incoming: Mapping[str, Probe | None],
    probes_existing: Mapping[str, Probe | None],
) -> QualitySignal:
    incoming_heights: list[int] = []
    existing_heights: list[int] = []
    incoming_rates: list[int] = []
    existing_rates: list[int] = []

    for item in overlap:
        probe = probes_incoming.get(item.candidate_id)
        if probe is not None:
            if probe.height:
                incoming_heights.append(probe.height)
            if probe.bit_rate:
                incoming_rates.append(probe.bit_rate)
        best = max(item.existing, key=lambda existing: existing.size_bytes)
        counterpart = probes_existing.get(best.key)
        if counterpart is not None:
            if counterpart.height:
                existing_heights.append(counterpart.height)
            if counterpart.bit_rate:
                existing_rates.append(counterpart.bit_rate)

    if not incoming_heights or not existing_heights:
        return QualitySignal.UNKNOWN
    incoming_height = statistics.median(incoming_heights)
    existing_height = statistics.median(existing_heights)
    if incoming_height > existing_height:
        return QualitySignal.BETTER
    if incoming_height < existing_height:
        return QualitySignal.WORSE

    if incoming_rates and existing_rates:
        rate = statistics.median(incoming_rates) / statistics.median(existing_rates)
        if rate > 1.1:
            return QualitySignal.BETTER
        if rate < 0.9:
            return QualitySignal.WORSE
    return QualitySignal.SAME


# ---- resolving and compiling -------------------------------------------


def resolve(
    decision: ReplacementDecision, resolution: ReplaceResolution
) -> ReplacementDecision:
    """Apply the user's choice to every group that was parked as MANUAL."""

    if resolution is ReplaceResolution.KEEP_BOTH:
        return dc_replace(decision, resolution=resolution)
    verdict = (
        GroupVerdict.REPLACE
        if resolution is ReplaceResolution.REPLACE
        else GroupVerdict.DISCARD
    )
    groups = tuple(
        dc_replace(group, verdict=verdict, reason=f"resolved_{resolution.value}")
        if group.verdict is GroupVerdict.MANUAL
        else group
        for group in decision.groups
    )
    return ReplacementDecision(groups=groups, resolution=resolution)


def apply_decision(plan: Plan, decision: ReplacementDecision, *, run_id: str) -> Plan:
    """Compile the decision into the plan as ordinary ledgered moves.

    Replacement inserts a trash move for every existing instance immediately
    before the incoming move that displaces it; discard rewrites the incoming
    move to end in the trash area instead of the library. Everything else —
    crash replay, revert, accounting — then falls out of the executor's
    existing machinery.
    """

    if decision.resolution is ReplaceResolution.KEEP_BOTH:
        return plan
    if decision.needs_confirmation:
        raise ReplaceError("unresolved_manual_groups")
    if not decision.changes_anything:
        return plan

    replace_pairs: dict[str, tuple[ExistingFile, ...]] = {}
    discard_ids: set[str] = set()
    discard_existing_names: set[str] = set()
    for group in decision.groups:
        if group.verdict is GroupVerdict.REPLACE:
            for item in group.overlap:
                replace_pairs[item.candidate_id] = item.existing
        elif group.verdict is GroupVerdict.DISCARD:
            for item in group.overlap:
                discard_ids.add(item.candidate_id)

    rename = next(
        (move for move in plan.moves if move.kind is MoveKind.FOLDER_RENAME), None
    )
    subtitle_names = set(decision.existing_subtitles)

    trashed: set[str] = set()
    moves: list[Move] = []
    for move in plan.moves:
        if move.kind is not MoveKind.MEDIA:
            moves.append(move)
            continue
        candidate_id = move.candidate_id or ""
        if candidate_id in replace_pairs:
            for existing in replace_pairs[candidate_id]:
                if existing.key in trashed:
                    continue
                trashed.add(existing.key)
                moves.append(_trash_existing(existing, rename, run_id))
            moves.append(move)
        elif candidate_id in discard_ids:
            moves.append(_trash_incoming(move, run_id))
        elif _is_duplicate_subtitle(move, discard_ids, decision, subtitle_names):
            moves.append(_trash_incoming(move, run_id))
        else:
            moves.append(move)

    return dc_replace(plan, moves=tuple(moves))


def _trash_existing(
    existing: ExistingFile, rename: Move | None, run_id: str
) -> Move:
    relative = existing.relative_path
    # A folder rename runs first, so a library file recorded under the old
    # folder name is displaced from its renamed location.
    if (
        rename is not None
        and existing.root is Root.LIBRARY
        and _first_part(relative) == rename.source_path
    ):
        parts = PurePosixPath(relative).parts
        relative = PurePosixPath(rename.dest_path, *parts[1:]).as_posix()
    return Move(
        kind=MoveKind.TRASH_REPLACED,
        source_root=existing.root,
        source_path=relative,
        dest_root=existing.root,
        dest_path=trash_relative(run_id, relative),
        extra_base=existing.extra_base,
    )


def _trash_incoming(move: Move, run_id: str) -> Move:
    return dc_replace(
        move,
        kind=MoveKind.TRASH_DUPLICATE,
        dest_root=Root.INBOUND,
        dest_path=trash_relative(run_id, move.source_path),
    )


def _is_duplicate_subtitle(
    move: Move,
    discard_ids: set[str],
    decision: ReplacementDecision,
    subtitle_names: set[str],
) -> bool:
    """A bundled subtitle mapped to a discarded episode is itself discarded
    only when the library already holds a subtitle of exactly that name —
    a subtitle the library lacks is still an additive win."""

    if PurePosixPath(move.dest_path).suffix.lower() in VIDEO_EXTENSIONS:
        return False
    if not discard_ids:
        return False
    span = span_from_name(PurePosixPath(move.dest_path).name)
    if span is None:
        return False
    for group in decision.groups:
        if group.verdict is not GroupVerdict.DISCARD:
            continue
        for item in group.overlap:
            if item.span is not None and _spans_pair(span, item.span):
                return PurePosixPath(move.dest_path).name in subtitle_names
    return False


def _first_part(relative: str) -> str:
    parts = PurePosixPath(relative).parts
    return parts[0] if parts else ""
