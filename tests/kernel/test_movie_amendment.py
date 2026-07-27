import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePosixPath

from reeloom.kernel.amendment import CompletedLayout, CompletedLayoutFile
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.movie_amendment import (
    MovieAmendmentPlan,
    compile_movie_amendment,
    verify_movie_amendment_bytes,
)
from reeloom.kernel.naming import MovieIdentity, SubtitleVariant
from reeloom.kernel.rename_plan import RootBinding


def _layout() -> CompletedLayout:
    return CompletedLayout(
        run_id="run-movie",
        original_plan_hash="sha256:" + "a" * 64,
        transaction_id="txn-v1-" + "b" * 64,
        root=RootBinding(PurePosixPath("/archive"), 1, 2),
        files=(
            CompletedLayoutFile(
                CandidateId(CandidateKind.VIDEO, 1),
                CandidateKind.VIDEO,
                PurePosixPath(
                    "旧电影 (2020) {tmdb-1}/旧电影 (2020).mkv"
                ),
                10,
                1,
                11,
                12,
                13,
                None,
            ),
            CompletedLayoutFile(
                CandidateId(CandidateKind.SUBTITLE, 1),
                CandidateKind.SUBTITLE,
                PurePosixPath(
                    "旧电影 (2020) {tmdb-1}/旧电影 (2020).chs.srt"
                ),
                5,
                1,
                21,
                22,
                23,
                "c" * 64,
            ),
        ),
    )


def _compile() -> MovieAmendmentPlan:
    plan = compile_movie_amendment(
        layout=_layout(),
        movie=MovieIdentity("新电影", 2024, 2),
        subtitle_variants=(
            (
                CandidateId(CandidateKind.SUBTITLE, 1),
                SubtitleVariant.CHS,
            ),
        ),
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    assert plan is not None
    return plan


def test_movie_amendment_round_trips_from_identity_and_variants() -> None:
    plan = _compile()

    restored = MovieAmendmentPlan.from_canonical_bytes(
        plan.canonical_bytes(),
        plan_hash=plan.plan_hash,
    )

    assert restored == plan
    assert {move.destination for move in restored.moves} == {
        PurePosixPath(
            "新电影 (2024) {tmdb-2}/新电影 (2024).mkv"
        ),
        PurePosixPath(
            "新电影 (2024) {tmdb-2}/新电影 (2024).chs.srt"
        ),
    }


def test_movie_amendment_round_trips_sparse_completed_candidate_ids() -> None:
    layout = _layout()
    sparse = CompletedLayout(
        run_id=layout.run_id,
        original_plan_hash=layout.original_plan_hash,
        transaction_id=layout.transaction_id,
        root=layout.root,
        files=(
            replace(
                layout.files[0],
                candidate_id=CandidateId(CandidateKind.VIDEO, 2),
            ),
            replace(
                layout.files[1],
                candidate_id=CandidateId(CandidateKind.SUBTITLE, 3),
            ),
        ),
    )
    plan = compile_movie_amendment(
        layout=sparse,
        movie=MovieIdentity("新电影", 2024, 2),
        subtitle_variants=(
            (
                CandidateId(CandidateKind.SUBTITLE, 3),
                SubtitleVariant.CHS,
            ),
        ),
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    assert plan is not None

    restored = MovieAmendmentPlan.from_canonical_bytes(
        plan.canonical_bytes(),
        plan_hash=plan.plan_hash,
    )

    assert restored == plan


def test_movie_amendment_rejects_rehashed_arbitrary_destination() -> None:
    payload = json.loads(_compile().canonical_bytes())
    payload["moves"][0]["destination"] = "attacker/choice.mkv"
    content = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    plan_hash = "sha256:" + hashlib.sha256(content).hexdigest()

    assert not verify_movie_amendment_bytes(content, plan_hash)


def test_movie_amendment_noop_does_not_create_plan() -> None:
    assert (
        compile_movie_amendment(
            layout=_layout(),
            movie=MovieIdentity("旧电影", 2020, 1),
            subtitle_variants=(
                (
                    CandidateId(CandidateKind.SUBTITLE, 1),
                    SubtitleVariant.CHS,
                ),
            ),
            created_at=datetime(2026, 7, 26, tzinfo=UTC),
        )
        is None
    )
