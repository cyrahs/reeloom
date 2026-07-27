from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError
from reeloom.kernel.file_types import candidate_kind_for_filename
from reeloom.kernel.plan import (
    CURRENT_PLAN_POLICY_VERSION,
    CURRENT_PLAN_SCHEMA_VERSION,
)
from reeloom.kernel.rename_plan import (
    CURRENT_RENAME_PLAN_POLICY_VERSION,
    CURRENT_RENAME_PLAN_SCHEMA_VERSION,
    RootBinding,
    verify_plan_bytes,
)
from reeloom.kernel.movie_plan import (
    MovieRenamePlan,
    verify_movie_plan_bytes,
)
from reeloom.kernel.amendment import (
    CURRENT_AMENDMENT_POLICY_VERSION,
    CURRENT_AMENDMENT_SCHEMA_VERSION,
    verify_amendment_bytes,
)
from reeloom.kernel.movie_amendment import (
    CURRENT_MOVIE_AMENDMENT_POLICY_VERSION,
    CURRENT_MOVIE_AMENDMENT_SCHEMA_VERSION,
    verify_movie_amendment_bytes,
)
from reeloom.kernel.schema import check_fields
from reeloom.kernel.tmdb import TmdbWorkType

_MAX_PLAN_BYTES = 4 * 1024 * 1024
_MAX_ITEMS = 10_000
_MAX_PATH_BYTES = 4096
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SEASON_PATTERN = re.compile(r"^S[0-9]{2,}$")
_TOP_FIELDS = frozenset(
    {
        "candidate_snapshot_id",
        "created_at",
        "draft_policy_version",
        "draft_schema_version",
        "mapping",
        "moves",
        "policy_version",
        "roots",
        "run_id",
        "schema_version",
        "series",
        "sources",
        "subtitle_variants",
        "unmapped_candidate_ids",
        "work_type",
    }
)
_AMENDMENT_FIELDS = frozenset(
    {
        "completed_transaction_id",
        "created_at",
        "moves",
        "parent_plan_hash",
        "policy_version",
        "roots",
        "run_id",
        "schema_version",
        "sources",
        "unmapped_candidate_ids",
    }
)
_MOVIE_AMENDMENT_FIELDS = frozenset(
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
_MOVIE_MOVE_FIELDS = frozenset(
    {"destination", "destination_preflight", "source_id", "video_id"}
)
_ROOT_FIELDS = frozenset({"device", "inode", "path"})
_ROOTS_FIELDS = frozenset({"output", "source"})
_SOURCE_FIELDS = frozenset(
    {
        "candidate_id",
        "ctime_ns",
        "device",
        "inode",
        "kind",
        "mtime_ns",
        "relative_path",
        "sample_digest",
        "size_bytes",
    }
)
_MOVE_FIELDS = frozenset(
    {
        "destination",
        "destination_preflight",
        "episode_end",
        "episode_start",
        "season",
        "source_id",
        "video_id",
    }
)


def _invalid_plan() -> ExecutorError:
    return ExecutorError(ExecutorErrorCode.INVALID_PLAN)


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid_plan()
    return value


def _require_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _invalid_plan()
    return value


def _require_list(value: object) -> list[object]:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        raise _invalid_plan()
    return cast(list[object], value)


def _relative_path(value: object) -> PurePosixPath:
    raw = _require_str(value)
    try:
        if (
            not raw
            or "\x00" in raw
            or "\\" in raw
            or len(raw.encode("utf-8")) > _MAX_PATH_BYTES
        ):
            raise _invalid_plan()
    except UnicodeEncodeError:
        raise _invalid_plan() from None
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or path.as_posix() != raw
        or not path.parts
        or any(
            part in {"", ".", ".."}
            or part.casefold().startswith(".env")
            for part in path.parts
        )
    ):
        raise _invalid_plan()
    return path


def _destination(value: object) -> PurePosixPath:
    path = _relative_path(value)
    if (
        len(path.parts) != 3
        or _SEASON_PATTERN.fullmatch(path.parts[1]) is None
        or any(len(part.encode("utf-8")) > 255 for part in path.parts)
    ):
        raise _invalid_plan()
    return path


def _movie_destination(value: object) -> PurePosixPath:
    path = _relative_path(value)
    if (
        len(path.parts) != 2
        or any(len(part.encode("utf-8")) > 255 for part in path.parts)
    ):
        raise _invalid_plan()
    return path


def _root(value: object) -> RootBinding:
    payload = check_fields(value, _ROOT_FIELDS, field="root")
    raw_path = _require_str(payload["path"])
    if (
        "\x00" in raw_path
        or any(
            unicodedata.category(character) == "Cs"
            for character in raw_path
        )
    ):
        raise _invalid_plan()
    path = PurePosixPath(raw_path)
    if path.anchor != "/" or path.as_posix() != raw_path:
        raise _invalid_plan()
    try:
        return RootBinding(
            path=path,
            device=_require_int(payload["device"]),
            inode=_require_int(payload["inode"]),
        )
    except DomainError:
        raise _invalid_plan() from None


@dataclass(frozen=True, slots=True)
class ExecutionSource:
    candidate_id: CandidateId
    kind: CandidateKind
    relative_path: PurePosixPath
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    sample_digest: str | None

    @classmethod
    def parse(cls, value: object) -> ExecutionSource:
        payload = check_fields(value, _SOURCE_FIELDS, field="source")
        try:
            candidate_id = CandidateId.parse(payload["candidate_id"])
            kind = CandidateKind(_require_str(payload["kind"]))
        except (DomainError, ValueError):
            raise _invalid_plan() from None
        digest = payload["sample_digest"]
        if (
            candidate_id.kind is not kind
            or (
                digest is not None
                and (
                    not isinstance(digest, str)
                    or _DIGEST_PATTERN.fullmatch(digest) is None
                )
            )
            or (kind is CandidateKind.SUBTITLE and digest is None)
            or (kind is CandidateKind.VIDEO and digest is not None)
        ):
            raise _invalid_plan()
        source = cls(
            candidate_id=candidate_id,
            kind=kind,
            relative_path=_relative_path(payload["relative_path"]),
            size_bytes=_require_int(payload["size_bytes"]),
            device=_require_int(payload["device"]),
            inode=_require_int(payload["inode"]),
            mtime_ns=_require_int(payload["mtime_ns"]),
            ctime_ns=_require_int(payload["ctime_ns"]),
            sample_digest=cast(str | None, digest),
        )
        if (
            candidate_kind_for_filename(source.relative_path.name)
            is not source.kind
        ):
            raise _invalid_plan()
        return source


@dataclass(frozen=True, slots=True)
class ExecutionMove:
    source_id: CandidateId
    video_id: CandidateId
    destination: PurePosixPath

    @classmethod
    def parse(cls, value: object) -> ExecutionMove:
        payload = check_fields(value, _MOVE_FIELDS, field="move")
        try:
            source_id = CandidateId.parse(payload["source_id"])
            video_id = CandidateId.parse(payload["video_id"])
        except DomainError:
            raise _invalid_plan() from None
        season = _require_int(payload["season"])
        episode_start = _require_int(payload["episode_start"])
        episode_end = _require_int(payload["episode_end"])
        if (
            payload["destination_preflight"] != "absent"
            or video_id.kind is not CandidateKind.VIDEO
            or episode_start < 1
            or episode_end < episode_start
            or season < 0
        ):
            raise _invalid_plan()
        return cls(
            source_id=source_id,
            video_id=video_id,
            destination=_destination(payload["destination"]),
        )


@dataclass(frozen=True, slots=True)
class ExecutionManifest:
    plan_hash: str
    run_id: str
    source_root: RootBinding
    output_root: RootBinding
    sources: tuple[ExecutionSource, ...]
    moves: tuple[ExecutionMove, ...]
    work_type: TmdbWorkType | None = None
    required_absent_directory: PurePosixPath | None = None

    @classmethod
    def from_canonical_bytes(
        cls,
        canonical_bytes: bytes,
        *,
        plan_hash: str,
    ) -> ExecutionManifest:
        if (
            not isinstance(canonical_bytes, bytes)
            or not 0 < len(canonical_bytes) <= _MAX_PLAN_BYTES
        ):
            raise _invalid_plan()
        if verify_movie_plan_bytes(canonical_bytes, plan_hash):
            return cls._from_movie_plan(
                canonical_bytes,
                plan_hash=plan_hash,
            )
        if verify_movie_amendment_bytes(canonical_bytes, plan_hash):
            return cls._from_movie_amendment(
                canonical_bytes,
                plan_hash=plan_hash,
            )
        amendment = verify_amendment_bytes(canonical_bytes, plan_hash)
        if not amendment and not verify_plan_bytes(
            canonical_bytes, plan_hash
        ):
            raise _invalid_plan()
        try:
            payload = check_fields(
                json.loads(
                    canonical_bytes.decode("utf-8"),
                    parse_constant=_reject_json_constant,
                ),
                _AMENDMENT_FIELDS if amendment else _TOP_FIELDS,
                field="plan",
            )
            if amendment:
                return cls._from_amendment_payload(
                    payload,
                    canonical_bytes=canonical_bytes,
                    plan_hash=plan_hash,
                )
            if (
                json.dumps(
                    payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                != canonical_bytes
                or payload["schema_version"]
                != CURRENT_RENAME_PLAN_SCHEMA_VERSION
                or payload["policy_version"]
                != CURRENT_RENAME_PLAN_POLICY_VERSION
                or payload["draft_schema_version"]
                != CURRENT_PLAN_SCHEMA_VERSION
                or payload["draft_policy_version"]
                != CURRENT_PLAN_POLICY_VERSION
                or not isinstance(payload["candidate_snapshot_id"], str)
                or not isinstance(payload["created_at"], str)
                or not isinstance(payload["mapping"], dict)
                or not isinstance(payload["series"], dict)
                or not isinstance(payload["subtitle_variants"], list)
                or payload["work_type"]
                not in {
                    TmdbWorkType.ANIME.value,
                    TmdbWorkType.TV_SERIES.value,
                }
            ):
                raise _invalid_plan()
            roots = check_fields(
                payload["roots"],
                _ROOTS_FIELDS,
                field="roots",
            )
            run_id = _require_str(payload["run_id"])
            if (
                not run_id
                or len(run_id.encode("utf-8")) > 128
                or any(
                    unicodedata.category(character).startswith("C")
                    for character in run_id
                )
            ):
                raise _invalid_plan()
            sources = tuple(
                ExecutionSource.parse(item)
                for item in _require_list(payload["sources"])
            )
            moves = tuple(
                ExecutionMove.parse(item)
                for item in _require_list(payload["moves"])
            )
            unmapped = tuple(
                CandidateId.parse(item)
                for item in _require_list(
                    payload["unmapped_candidate_ids"]
                )
            )
            manifest = cls(
                plan_hash=plan_hash,
                run_id=run_id,
                source_root=_root(roots["source"]),
                output_root=_root(roots["output"]),
                sources=sources,
                moves=moves,
                work_type=TmdbWorkType(
                    _require_str(payload["work_type"])
                ),
            )
            manifest._validate_partition(unmapped)
            return manifest
        except ExecutorError:
            raise
        except (
            DomainError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            UnicodeEncodeError,
            ValueError,
        ):
            raise _invalid_plan() from None

    @classmethod
    def _from_movie_amendment(
        cls,
        canonical_bytes: bytes,
        *,
        plan_hash: str,
    ) -> ExecutionManifest:
        try:
            payload = check_fields(
                json.loads(
                    canonical_bytes.decode("ascii"),
                    parse_constant=_reject_json_constant,
                ),
                _MOVIE_AMENDMENT_FIELDS,
                field="movie_amendment",
            )
            if (
                json.dumps(
                    payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("ascii")
                != canonical_bytes
                or payload["schema_version"]
                != CURRENT_MOVIE_AMENDMENT_SCHEMA_VERSION
                or payload["policy_version"]
                != CURRENT_MOVIE_AMENDMENT_POLICY_VERSION
                or payload["work_type"] != TmdbWorkType.MOVIE.value
                or not isinstance(payload["created_at"], str)
                or not isinstance(payload["parent_plan_hash"], str)
                or not isinstance(
                    payload["completed_transaction_id"], str
                )
            ):
                raise _invalid_plan()
            roots = check_fields(
                payload["roots"],
                _ROOTS_FIELDS,
                field="roots",
            )
            source_root = _root(roots["source"])
            output_root = _root(roots["output"])
            if source_root != output_root:
                raise _invalid_plan()
            sources = tuple(
                ExecutionSource.parse(item)
                for item in _require_list(payload["sources"])
            )
            moves: list[ExecutionMove] = []
            for item in _require_list(payload["moves"]):
                move = check_fields(
                    item,
                    _MOVIE_MOVE_FIELDS,
                    field="movie_move",
                )
                if move["destination_preflight"] != "absent":
                    raise _invalid_plan()
                moves.append(
                    ExecutionMove(
                        source_id=CandidateId.parse(move["source_id"]),
                        video_id=CandidateId.parse(move["video_id"]),
                        destination=_movie_destination(
                            move["destination"]
                        ),
                    )
                )
            unchanged = tuple(
                CandidateId.parse(item)
                for item in _require_list(
                    payload["unchanged_candidate_ids"]
                )
            )
            source_roots = {
                item.relative_path.parts[0] for item in sources
            }
            destination_roots = {
                item.destination.parts[0] for item in moves
            }
            required_absent = (
                PurePosixPath(next(iter(destination_roots)))
                if len(destination_roots) == 1
                and destination_roots.isdisjoint(source_roots)
                else None
            )
            manifest = cls(
                plan_hash=plan_hash,
                run_id=_require_str(payload["run_id"]),
                source_root=source_root,
                output_root=output_root,
                sources=sources,
                moves=tuple(moves),
                work_type=TmdbWorkType.MOVIE,
                required_absent_directory=required_absent,
            )
            manifest._validate_partition(
                unchanged,
                allow_unmoved_video_reference=True,
            )
            return manifest
        except ExecutorError:
            raise
        except (
            DomainError,
            json.JSONDecodeError,
            UnicodeError,
            ValueError,
        ):
            raise _invalid_plan() from None

    @classmethod
    def _from_movie_plan(
        cls,
        canonical_bytes: bytes,
        *,
        plan_hash: str,
    ) -> ExecutionManifest:
        try:
            plan = MovieRenamePlan.from_canonical_bytes(
                canonical_bytes,
                plan_hash=plan_hash,
            )
            sources = tuple(
                ExecutionSource(
                    candidate_id=item.candidate_id,
                    kind=item.kind,
                    relative_path=item.relative_path,
                    size_bytes=item.size_bytes,
                    device=item.device,
                    inode=item.inode,
                    mtime_ns=item.mtime_ns,
                    ctime_ns=item.ctime_ns,
                    sample_digest=item.sample_digest,
                )
                for item in plan.sources
            )
            for source in sources:
                if (
                    candidate_kind_for_filename(source.relative_path.name)
                    is not source.kind
                ):
                    raise _invalid_plan()
            moves = tuple(
                ExecutionMove(
                    source_id=item.source_id,
                    video_id=item.video_id,
                    destination=_movie_destination(
                        item.destination.as_posix()
                    ),
                )
                for item in plan.draft.moves
            )
            movie_root = PurePosixPath(moves[0].destination.parts[0])
            manifest = cls(
                plan_hash=plan_hash,
                run_id=plan.run_id,
                source_root=plan.source_root,
                output_root=plan.output_root,
                sources=sources,
                moves=moves,
                work_type=TmdbWorkType.MOVIE,
                required_absent_directory=movie_root,
            )
            manifest._validate_partition(
                plan.draft.unmapped_candidate_ids
            )
            return manifest
        except ExecutorError:
            raise
        except (DomainError, ValueError, IndexError):
            raise _invalid_plan() from None

    @classmethod
    def _from_amendment_payload(
        cls,
        payload: dict[str, object],
        *,
        canonical_bytes: bytes,
        plan_hash: str,
    ) -> ExecutionManifest:
        if (
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            != canonical_bytes
            or payload["schema_version"]
            != CURRENT_AMENDMENT_SCHEMA_VERSION
            or payload["policy_version"]
            != CURRENT_AMENDMENT_POLICY_VERSION
            or not isinstance(payload["created_at"], str)
            or not isinstance(payload["parent_plan_hash"], str)
            or not isinstance(payload["completed_transaction_id"], str)
        ):
            raise _invalid_plan()
        roots = check_fields(
            payload["roots"],
            _ROOTS_FIELDS,
            field="roots",
        )
        source_root = _root(roots["source"])
        output_root = _root(roots["output"])
        if source_root != output_root:
            raise _invalid_plan()
        manifest = cls(
            plan_hash=plan_hash,
            run_id=_require_str(payload["run_id"]),
            source_root=source_root,
            output_root=output_root,
            sources=tuple(
                ExecutionSource.parse(item)
                for item in _require_list(payload["sources"])
            ),
            moves=tuple(
                ExecutionMove.parse(item)
                for item in _require_list(payload["moves"])
            ),
        )
        unmapped = tuple(
            CandidateId.parse(item)
            for item in _require_list(payload["unmapped_candidate_ids"])
        )
        manifest._validate_partition(
            unmapped,
            allow_unmoved_video_reference=True,
        )
        return manifest

    def _validate_partition(
        self,
        unmapped: tuple[CandidateId, ...],
        *,
        allow_unmoved_video_reference: bool = False,
    ) -> None:
        source_by_id = {
            source.candidate_id: source for source in self.sources
        }
        source_paths = {
            source.relative_path for source in self.sources
        }
        move_ids = tuple(move.source_id for move in self.moves)
        collision_keys = {
            tuple(
                unicodedata.normalize("NFKC", part).casefold()
                for part in move.destination.parts
            )
            for move in self.moves
        }
        if (
            not self.sources
            or len(source_by_id) != len(self.sources)
            or len(source_paths) != len(self.sources)
            or len(set(move_ids)) != len(move_ids)
            or len(set(unmapped)) != len(unmapped)
            or set(move_ids) & set(unmapped)
            or set(move_ids) | set(unmapped) != set(source_by_id)
            or len(collision_keys) != len(self.moves)
        ):
            raise _invalid_plan()
        moved_videos = {
            move.source_id
            for move in self.moves
            if move.source_id.kind is CandidateKind.VIDEO
        }
        for move in self.moves:
            source = source_by_id.get(move.source_id)
            if (
                source is None
                or candidate_kind_for_filename(move.destination.name)
                is not source.kind
                or move.destination.suffix.casefold()
                != source.relative_path.suffix.casefold()
                or (
                    source.kind is CandidateKind.VIDEO
                    and move.video_id != source.candidate_id
                )
                or (
                    source.kind is CandidateKind.SUBTITLE
                    and move.video_id not in (
                        set(source_by_id)
                        if allow_unmoved_video_reference
                        else moved_videos
                    )
                )
            ):
                raise _invalid_plan()
