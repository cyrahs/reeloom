from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.plan_review import (
    PlanReview,
    PlanReviewItem,
    PlanReviewReason,
    PlanReviewStatus,
    PlanReviewVerification,
    merge_plan_reviews,
    normalize_plan_review,
)


def test_review_verifies_only_observed_inventory_conflicts() -> None:
    candidates = tuple(
        CandidateId.parse(value)
        for value in ("video:1", "video:2", "subtitle:1")
    )
    review = normalize_plan_review(
        {
            "summary": "第 2 个视频与已有特别篇冲突。",
            "unmapped_explanations": [
                {
                    "candidate_id": "video:2",
                    "reason": "existing_episode",
                    "detail": "识别为 S00E03。",
                    "season": 0,
                    "episode": 3,
                    "related_video_id": None,
                },
                {
                    "candidate_id": "subtitle:1",
                    "reason": "not_selected",
                    "detail": "跟随未映射正片。",
                    "season": None,
                    "episode": None,
                    "related_video_id": "video:2",
                },
            ],
        },
        candidate_ids=candidates,
        mapped_ids=frozenset({CandidateId.parse("video:1")}),
        verified_conflicts=(
            (CandidateId.parse("video:2"), 0, 3),
        ),
    )

    assert review.status is PlanReviewStatus.AGENT_AND_SYSTEM
    assert review.items[0].reason is PlanReviewReason.EXISTING_EPISODE
    assert (
        review.items[0].verification
        is PlanReviewVerification.VERIFIED
    )
    assert review.items[1].related_video_id == CandidateId.parse(
        "video:2"
    )
    assert PlanReview.from_dict(review.to_dict()) == review


def test_invalid_review_parts_fall_back_without_rejecting_mapping() -> None:
    candidates = (
        CandidateId.parse("video:1"),
        CandidateId.parse("video:2"),
    )
    review = normalize_plan_review(
        {
            "summary": "x" * 5000,
            "unmapped_explanations": [
                {
                    "candidate_id": "video:2",
                    "reason": "existing_episode",
                    "detail": "unverified",
                    "season": 0,
                    "episode": 9,
                    "related_video_id": None,
                },
                {
                    "candidate_id": "video:1",
                    "reason": "extra_video",
                    "detail": None,
                    "season": None,
                    "episode": None,
                    "related_video_id": None,
                },
                {
                    "candidate_id": "../invalid",
                    "reason": "other",
                    "detail": None,
                    "season": None,
                    "episode": None,
                    "related_video_id": None,
                },
            ],
        },
        candidate_ids=candidates,
        mapped_ids=frozenset({CandidateId.parse("video:1")}),
    )

    assert review.status is PlanReviewStatus.AGENT_AND_SYSTEM
    assert review.agent_summary is None
    assert len(review.items) == 1
    assert (
        review.items[0].verification
        is PlanReviewVerification.ADVISORY
    )


def test_system_conflict_evidence_fills_a_partial_stored_review() -> None:
    candidate_id = CandidateId.parse("video:2")
    stored = normalize_plan_review(
        {
            "summary": "第 2 个视频没有进入映射。",
            "unmapped_explanations": [],
        },
        candidate_ids=(candidate_id,),
        mapped_ids=frozenset(),
    )
    system = PlanReview.system_only(
        items=(
            PlanReviewItem(
                candidate_id=candidate_id,
                reason=PlanReviewReason.EXISTING_EPISODE,
                verification=PlanReviewVerification.VERIFIED,
                season=0,
                episode=3,
            ),
        )
    )

    merged = merge_plan_reviews(stored, system)

    assert merged.status is PlanReviewStatus.AGENT_AND_SYSTEM
    assert merged.agent_summary == "第 2 个视频没有进入映射。"
    assert merged.items == system.items


def test_unrelated_inventory_tuple_does_not_verify_agent_association() -> None:
    candidate_id = CandidateId.parse("video:2")
    review = normalize_plan_review(
        {
            "summary": None,
            "unmapped_explanations": [
                {
                    "candidate_id": "video:2",
                    "reason": "existing_episode",
                    "detail": "判断为 S00E03。",
                    "season": 0,
                    "episode": 3,
                    "related_video_id": None,
                }
            ],
        },
        candidate_ids=(candidate_id,),
        mapped_ids=frozenset(),
        verified_conflicts=(
            (CandidateId.parse("video:1"), 0, 3),
        ),
    )

    assert (
        review.items[0].verification
        is PlanReviewVerification.ADVISORY
    )


def test_exact_conflict_overrides_an_unverified_agent_reason() -> None:
    candidate_id = CandidateId.parse("video:2")
    review = normalize_plan_review(
        {
            "summary": None,
            "unmapped_explanations": [
                {
                    "candidate_id": "video:2",
                    "reason": "extra_video",
                    "detail": "没有选中。",
                    "season": None,
                    "episode": None,
                    "related_video_id": None,
                }
            ],
        },
        candidate_ids=(candidate_id,),
        mapped_ids=frozenset(),
        verified_conflicts=((candidate_id, 0, 3),),
    )

    assert review.items[0].reason is PlanReviewReason.EXISTING_EPISODE
    assert (
        review.items[0].verification
        is PlanReviewVerification.VERIFIED
    )
    assert review.items[0].agent_detail == "没有选中。"


def test_oversized_agent_payload_retains_bounded_system_evidence() -> None:
    candidates = tuple(
        CandidateId.parse(f"video:{ordinal}")
        for ordinal in range(1, 129)
    )
    review = normalize_plan_review(
        {
            "summary": None,
            "unmapped_explanations": [
                {
                    "candidate_id": str(candidate_id),
                    "reason": "extra_video",
                    "detail": "x" * 800,
                    "season": None,
                    "episode": None,
                    "related_video_id": None,
                }
                for candidate_id in candidates
            ],
        },
        candidate_ids=candidates,
        mapped_ids=frozenset(),
        verified_conflicts=((candidates[-1], 0, 3),),
    )

    assert review.status is PlanReviewStatus.SYSTEM_ONLY
    assert len(review.items) == 1
    assert review.items[0].candidate_id == candidates[-1]
