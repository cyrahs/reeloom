import hashlib
import json
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.errors import DomainError
from reeloom.kernel.movie import (
    MovieMappingDraft,
    compile_movie_plan_draft,
)
from reeloom.kernel.movie_plan import MovieRenamePlan
from reeloom.kernel.naming import MovieIdentity, SubtitleVariant
from reeloom.kernel.rename_plan import RootBinding
from reeloom.kernel.scanner import ScannedFile, build_candidate_snapshot


def _plan() -> MovieRenamePlan:
    snapshot = build_candidate_snapshot(
        (
            ScannedFile(
                PurePosixPath("movie.mkv"),
                CandidateKind.VIDEO,
                10,
                1,
                11,
                12,
                13,
            ),
            ScannedFile(
                PurePosixPath("movie.ass"),
                CandidateKind.SUBTITLE,
                5,
                1,
                21,
                22,
                23,
                "a" * 64,
            ),
        )
    )
    mapping = MovieMappingDraft.from_dict(
        {"video_id": "video:1", "subtitle_ids": ["subtitle:1"]},
        candidates=snapshot.candidates,
    )
    variants = ((mapping.subtitle_ids[0], SubtitleVariant.CHS),)
    draft = compile_movie_plan_draft(
        movie=MovieIdentity("电影", 2024, 99),
        mapping=mapping,
        candidates=snapshot,
        subtitle_variants=variants,
    )
    return MovieRenamePlan.create(
        run_id="run-movie",
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
        source_root=RootBinding(PurePosixPath("/watch"), 1, 2),
        output_root=RootBinding(PurePosixPath("/archive"), 1, 3),
        candidate_snapshot=snapshot,
        subtitle_variants=variants,
        draft=draft,
        checked_destinations=tuple(
            item.destination for item in draft.moves
        ),
    )


def test_movie_plan_round_trips_and_exposes_relative_preview() -> None:
    plan = _plan()
    restored = MovieRenamePlan.from_canonical_bytes(
        plan.canonical_bytes(),
        plan_hash=plan.plan_hash,
    )

    assert restored == plan
    assert all(
        not item.destination.is_absolute()
        for item in restored.preview.moves
    )


def test_movie_plan_rejects_semantic_tamper_with_new_hash() -> None:
    plan = _plan()
    payload = json.loads(plan.canonical_bytes())
    payload["moves"][0]["destination"] = "attacker/choice.mkv"
    content = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    plan_hash = "sha256:" + hashlib.sha256(content).hexdigest()

    with pytest.raises(DomainError):
        MovieRenamePlan.from_canonical_bytes(
            content,
            plan_hash=plan_hash,
        )
