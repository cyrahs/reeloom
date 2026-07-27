from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

from reeloom.kernel.amendment import CompletedLayout, CompletedLayoutFile
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.naming import (
    MovieIdentity,
    SubtitleVariant,
    movie_subtitle_relative_path,
    movie_video_relative_path,
)
from reeloom.kernel.rename_plan import (
    RootBinding,
    _canonical_timestamp,
    _parse_root,
    _parse_sources,
    _reject_duplicate_keys,
    _require_list,
    _require_str,
    _root_payload,
    _source_payload,
    verify_plan_bytes,
)
from reeloom.kernel.schema import check_fields
from reeloom.kernel.tmdb import TmdbWorkType

CURRENT_MOVIE_AMENDMENT_SCHEMA_VERSION = "movie-amendment-v1"
CURRENT_MOVIE_AMENDMENT_POLICY_VERSION = "m10-v1"
_MAX_CANONICAL_BYTES = 8 * 1024 * 1024
_FIELDS = frozenset(
    {
        "completed_transaction_id",
        "created_at",
        "moves",
        "movie",
        "parent_plan_hash",
        "policy_version",
        "roots",
        "run_id",
        "schema_version",
        "sources",
        "subtitle_variants",
        "unchanged_candidate_ids",
        "work_type",
    }
)
_ROOTS_FIELDS = frozenset({"output", "source"})
_VARIANT_FIELDS = frozenset({"subtitle_id", "variant"})


def _sort_key(candidate_id: CandidateId) -> tuple[int, int]:
    return (
        0 if candidate_id.kind is CandidateKind.VIDEO else 1,
        candidate_id.ordinal,
    )


@dataclass(frozen=True, slots=True)
class MovieDesiredLayoutMove:
    source_id: CandidateId
    video_id: CandidateId
    destination: PurePosixPath

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_id, CandidateId)
            or not isinstance(self.video_id, CandidateId)
            or self.video_id.kind is not CandidateKind.VIDEO
            or not isinstance(self.destination, PurePosixPath)
            or self.destination.is_absolute()
            or len(self.destination.parts) != 2
            or ".." in self.destination.parts
            or any(
                part in {"", ".", ".."}
                or part.casefold().startswith(".env")
                or len(part.encode("utf-8")) > 255
                for part in self.destination.parts
            )
        ):
            raise DomainError(ErrorCode.INVALID_DESTINATION)


@dataclass(frozen=True, slots=True)
class MovieAmendmentPlan:
    plan_hash: str
    run_id: str
    parent_plan_hash: str
    completed_transaction_id: str
    created_at: datetime
    source_root: RootBinding
    output_root: RootBinding
    movie: MovieIdentity
    sources: tuple[CompletedLayoutFile, ...]
    subtitle_variants: tuple[
        tuple[CandidateId, SubtitleVariant], ...
    ]
    moves: tuple[MovieDesiredLayoutMove, ...]

    @classmethod
    def from_canonical_bytes(
        cls,
        content: bytes,
        *,
        plan_hash: str,
    ) -> MovieAmendmentPlan:
        if (
            not isinstance(content, bytes)
            or not 0 < len(content) <= _MAX_CANONICAL_BYTES
            or not verify_plan_bytes(content, plan_hash)
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        try:
            payload = check_fields(
                json.loads(
                    content,
                    object_pairs_hook=_reject_duplicate_keys,
                ),
                _FIELDS,
                field="movie_amendment",
            )
        except (UnicodeError, json.JSONDecodeError, ValueError):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None
        if (
            payload["schema_version"]
            != CURRENT_MOVIE_AMENDMENT_SCHEMA_VERSION
            or payload["policy_version"]
            != CURRENT_MOVIE_AMENDMENT_POLICY_VERSION
            or payload["work_type"] != TmdbWorkType.MOVIE.value
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        roots = check_fields(payload["roots"], _ROOTS_FIELDS, field="roots")
        source_root = _parse_root(roots["source"], field="roots.source")
        output_root = _parse_root(roots["output"], field="roots.output")
        if source_root != output_root:
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
        snapshot = _parse_sources(
            payload["sources"],
            preserve_candidate_ids=True,
        )
        files = tuple(
            CompletedLayoutFile(
                candidate_id=item.candidate.id,
                kind=item.candidate.kind,
                relative_path=item.relative_path,
                size_bytes=item.size_bytes,
                device=item.device,
                inode=item.inode,
                mtime_ns=item.mtime_ns,
                ctime_ns=item.ctime_ns,
                sample_digest=item.sample_digest,
            )
            for item in snapshot.records
        )
        variants = _parse_variants(payload["subtitle_variants"])
        created_at_text = _require_str(
            payload["created_at"],
            field="created_at",
        )
        try:
            created_at = datetime.fromisoformat(
                created_at_text.replace("Z", "+00:00")
            )
        except ValueError:
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None
        layout = CompletedLayout(
            run_id=_require_str(payload["run_id"], field="run_id"),
            original_plan_hash=_require_str(
                payload["parent_plan_hash"],
                field="parent_plan_hash",
            ),
            transaction_id=_require_str(
                payload["completed_transaction_id"],
                field="completed_transaction_id",
            ),
            root=source_root,
            files=files,
        )
        plan = compile_movie_amendment(
            layout=layout,
            movie=MovieIdentity.from_dict(payload["movie"]),
            subtitle_variants=variants,
            created_at=created_at,
        )
        if (
            plan is None
            or plan.plan_hash != plan_hash
            or plan.canonical_bytes() != content
        ):
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
        return plan

    def _payload(self) -> dict[str, object]:
        moved = {item.source_id for item in self.moves}
        return {
            "completed_transaction_id": self.completed_transaction_id,
            "created_at": _canonical_timestamp(self.created_at),
            "moves": [
                {
                    "destination": item.destination.as_posix(),
                    "destination_preflight": "absent",
                    "source_id": str(item.source_id),
                    "video_id": str(item.video_id),
                }
                for item in self.moves
            ],
            "movie": {
                "release_year": self.movie.release_year,
                "title_zh_cn": self.movie.title_zh_cn,
                "tmdb_id": self.movie.tmdb_id,
            },
            "parent_plan_hash": self.parent_plan_hash,
            "policy_version": CURRENT_MOVIE_AMENDMENT_POLICY_VERSION,
            "roots": {
                "output": _root_payload(self.output_root),
                "source": _root_payload(self.source_root),
            },
            "run_id": self.run_id,
            "schema_version": CURRENT_MOVIE_AMENDMENT_SCHEMA_VERSION,
            "sources": [_source_payload(item) for item in self.sources],
            "subtitle_variants": [
                {
                    "subtitle_id": str(candidate_id),
                    "variant": variant.value,
                }
                for candidate_id, variant in self.subtitle_variants
            ],
            "unchanged_candidate_ids": [
                str(item.candidate_id)
                for item in self.sources
                if item.candidate_id not in moved
            ],
            "work_type": TmdbWorkType.MOVIE.value,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self._payload(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    def verify_hash(self) -> bool:
        return verify_plan_bytes(self.canonical_bytes(), self.plan_hash)


def _parse_variants(
    value: object,
) -> tuple[tuple[CandidateId, SubtitleVariant], ...]:
    variants: list[tuple[CandidateId, SubtitleVariant]] = []
    for index, item in enumerate(
        _require_list(value, field="subtitle_variants")
    ):
        payload = check_fields(
            item,
            _VARIANT_FIELDS,
            field=f"subtitle_variants[{index}]",
        )
        try:
            variant = SubtitleVariant(
                _require_str(payload["variant"], field="variant")
            )
        except ValueError:
            raise DomainError(ErrorCode.INVALID_SUBTITLE_VARIANT) from None
        variants.append(
            (CandidateId.parse(payload["subtitle_id"]), variant)
        )
    return tuple(variants)


def _desired_moves(
    *,
    layout: CompletedLayout,
    movie: MovieIdentity,
    subtitle_variants: tuple[
        tuple[CandidateId, SubtitleVariant], ...
    ],
) -> tuple[MovieDesiredLayoutMove, ...]:
    videos = tuple(
        item for item in layout.files if item.kind is CandidateKind.VIDEO
    )
    subtitles = tuple(
        item for item in layout.files if item.kind is CandidateKind.SUBTITLE
    )
    variants = dict(subtitle_variants)
    if (
        len(videos) != 1
        or len(variants) != len(subtitle_variants)
        or set(variants)
        != {item.candidate_id for item in subtitles}
    ):
        raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
    video_id = videos[0].candidate_id
    moves = [
        MovieDesiredLayoutMove(
            source_id=video_id,
            video_id=video_id,
            destination=movie_video_relative_path(
                movie,
                videos[0].relative_path.suffix,
            ),
        )
    ]
    grouped: dict[PurePosixPath, list[CompletedLayoutFile]] = {}
    for source in subtitles:
        destination = movie_subtitle_relative_path(
            movie,
            variants[source.candidate_id],
            source.relative_path.suffix,
        )
        grouped.setdefault(destination, []).append(source)
    for destination, group in grouped.items():
        ordered = sorted(group, key=lambda item: _sort_key(item.candidate_id))
        for index, source in enumerate(ordered, start=1):
            numbered = (
                destination
                if len(ordered) == 1
                else destination.with_name(
                    f"{destination.stem}.{index}{destination.suffix}"
                )
            )
            moves.append(
                MovieDesiredLayoutMove(
                    source_id=source.candidate_id,
                    video_id=video_id,
                    destination=numbered,
                )
            )
    return tuple(
        sorted(moves, key=lambda item: _sort_key(item.source_id))
    )


def compile_movie_amendment(
    *,
    layout: CompletedLayout,
    movie: MovieIdentity,
    subtitle_variants: tuple[
        tuple[CandidateId, SubtitleVariant], ...
    ],
    created_at: datetime,
) -> MovieAmendmentPlan | None:
    if (
        not isinstance(layout, CompletedLayout)
        or not isinstance(movie, MovieIdentity)
        or not isinstance(subtitle_variants, tuple)
        or any(
            not isinstance(candidate_id, CandidateId)
            or not isinstance(variant, SubtitleVariant)
            for candidate_id, variant in subtitle_variants
        )
        or not isinstance(created_at, datetime)
        or created_at.tzinfo is None
    ):
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
    desired = _desired_moves(
        layout=layout,
        movie=movie,
        subtitle_variants=subtitle_variants,
    )
    files = {item.candidate_id: item for item in layout.files}
    destinations = tuple(item.destination for item in desired)
    collision_keys = {
        tuple(
            unicodedata.normalize("NFKC", part).casefold()
            for part in destination.parts
        )
        for destination in destinations
    }
    if len(collision_keys) != len(destinations):
        raise DomainError(ErrorCode.DESTINATION_COLLISION)
    occupied = {
        item.relative_path: item.candidate_id for item in layout.files
    }
    changed: list[MovieDesiredLayoutMove] = []
    for item in desired:
        source = files[item.source_id]
        if item.destination == source.relative_path:
            continue
        occupant = occupied.get(item.destination)
        if occupant is not None and occupant != item.source_id:
            raise DomainError(ErrorCode.DESTINATION_COLLISION)
        changed.append(item)
    if not changed:
        return None
    provisional = MovieAmendmentPlan(
        plan_hash="",
        run_id=layout.run_id,
        parent_plan_hash=layout.original_plan_hash,
        completed_transaction_id=layout.transaction_id,
        created_at=created_at,
        source_root=layout.root,
        output_root=layout.root,
        movie=movie,
        sources=layout.files,
        subtitle_variants=tuple(
            sorted(subtitle_variants, key=lambda item: _sort_key(item[0]))
        ),
        moves=tuple(changed),
    )
    plan_hash = "sha256:" + hashlib.sha256(
        provisional.canonical_bytes()
    ).hexdigest()
    return MovieAmendmentPlan(
        plan_hash=plan_hash,
        run_id=provisional.run_id,
        parent_plan_hash=provisional.parent_plan_hash,
        completed_transaction_id=provisional.completed_transaction_id,
        created_at=provisional.created_at,
        source_root=provisional.source_root,
        output_root=provisional.output_root,
        movie=provisional.movie,
        sources=provisional.sources,
        subtitle_variants=provisional.subtitle_variants,
        moves=provisional.moves,
    )


def verify_movie_amendment_bytes(
    content: bytes,
    plan_hash: str,
) -> bool:
    try:
        MovieAmendmentPlan.from_canonical_bytes(
            content,
            plan_hash=plan_hash,
        )
    except (DomainError, ValueError):
        return False
    return True
