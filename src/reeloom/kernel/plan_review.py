from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError
from reeloom.kernel.schema import check_fields

PLAN_REVIEW_SCHEMA = "plan-review-v1"
MAX_REVIEW_BYTES = 64 * 1024
MAX_REVIEW_ITEMS = 128
MAX_REVIEW_SUMMARY_BYTES = 4 * 1024
MAX_REVIEW_DETAIL_BYTES = 1024


class PlanReviewStatus(StrEnum):
    AGENT_AND_SYSTEM = "agent_and_system"
    SYSTEM_ONLY = "system_only"
    UNAVAILABLE = "unavailable"


class PlanReviewReason(StrEnum):
    EXISTING_EPISODE = "existing_episode"
    POSSIBLE_EXISTING_MOVIE = "possible_existing_movie"
    EXTRA_VIDEO = "extra_video"
    AMBIGUOUS_MAPPING = "ambiguous_mapping"
    UNSUPPORTED_CONTENT = "unsupported_content"
    DUPLICATE_CANDIDATE = "duplicate_candidate"
    NOT_SELECTED = "not_selected"
    OTHER = "other"


class PlanReviewVerification(StrEnum):
    VERIFIED = "verified"
    ADVISORY = "advisory"
    FALLBACK = "fallback"


def _bounded_optional_text(
    value: object,
    *,
    max_bytes: int,
) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > max_bytes
    ):
        raise ValueError("invalid review text")
    return value


@dataclass(frozen=True, slots=True)
class PlanReviewItem:
    candidate_id: CandidateId
    reason: PlanReviewReason
    verification: PlanReviewVerification
    agent_detail: str | None = None
    season: int | None = None
    episode: int | None = None
    related_video_id: CandidateId | None = None

    def __post_init__(self) -> None:
        _bounded_optional_text(
            self.agent_detail,
            max_bytes=MAX_REVIEW_DETAIL_BYTES,
        )
        if (
            (self.season is None) != (self.episode is None)
            or (
                self.season is not None
                and (
                    type(self.season) is not int
                    or not 0 <= self.season <= 999
                    or type(self.episode) is not int
                    or not 1 <= self.episode <= 100_000
                )
            )
            or (
                self.related_video_id is not None
                and (
                    self.candidate_id.kind is not CandidateKind.SUBTITLE
                    or self.related_video_id.kind is not CandidateKind.VIDEO
                )
            )
            or (
                self.verification is PlanReviewVerification.VERIFIED
                and (
                    self.reason is not PlanReviewReason.EXISTING_EPISODE
                    or self.season is None
                )
            )
        ):
            raise ValueError("invalid plan review item")

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_detail": self.agent_detail,
            "candidate_id": str(self.candidate_id),
            "episode": self.episode,
            "reason": self.reason.value,
            "related_video_id": (
                None
                if self.related_video_id is None
                else str(self.related_video_id)
            ),
            "season": self.season,
            "verification": self.verification.value,
        }

    @classmethod
    def from_dict(cls, value: object) -> PlanReviewItem:
        raw = check_fields(
            value,
            frozenset(
                {
                    "agent_detail",
                    "candidate_id",
                    "episode",
                    "reason",
                    "related_video_id",
                    "season",
                    "verification",
                }
            ),
            field="plan_review_item",
        )
        related = raw["related_video_id"]
        return cls(
            candidate_id=CandidateId.parse(raw["candidate_id"]),
            reason=PlanReviewReason(raw["reason"]),
            verification=PlanReviewVerification(raw["verification"]),
            agent_detail=_bounded_optional_text(
                raw["agent_detail"],
                max_bytes=MAX_REVIEW_DETAIL_BYTES,
            ),
            season=raw["season"],
            episode=raw["episode"],
            related_video_id=(
                None if related is None else CandidateId.parse(related)
            ),
        )


@dataclass(frozen=True, slots=True)
class PlanReview:
    status: PlanReviewStatus
    agent_summary: str | None = None
    items: tuple[PlanReviewItem, ...] = ()

    def __post_init__(self) -> None:
        _bounded_optional_text(
            self.agent_summary,
            max_bytes=MAX_REVIEW_SUMMARY_BYTES,
        )
        if (
            len(self.items) > MAX_REVIEW_ITEMS
            or len({item.candidate_id for item in self.items})
            != len(self.items)
            or (
                self.status is not PlanReviewStatus.AGENT_AND_SYSTEM
                and self.agent_summary is not None
            )
            or (
                self.status is PlanReviewStatus.UNAVAILABLE
                and bool(self.items)
            )
            or (
                self.status is PlanReviewStatus.SYSTEM_ONLY
                and any(
                    item.verification
                    is not PlanReviewVerification.VERIFIED
                    or item.agent_detail is not None
                    for item in self.items
                )
            )
            or len(self.canonical_bytes()) > MAX_REVIEW_BYTES
        ):
            raise ValueError("invalid plan review")

    @classmethod
    def system_only(
        cls,
        *,
        items: tuple[PlanReviewItem, ...] = (),
    ) -> PlanReview:
        return cls(status=PlanReviewStatus.SYSTEM_ONLY, items=items)

    @classmethod
    def unavailable(cls) -> PlanReview:
        return cls(status=PlanReviewStatus.UNAVAILABLE)

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_summary": self.agent_summary,
            "items": [item.to_dict() for item in self.items],
            "schema_version": PLAN_REVIEW_SCHEMA,
            "status": self.status.value,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    @classmethod
    def from_dict(cls, value: object) -> PlanReview:
        raw = check_fields(
            value,
            frozenset(
                {"agent_summary", "items", "schema_version", "status"}
            ),
            field="plan_review",
        )
        if raw["schema_version"] != PLAN_REVIEW_SCHEMA:
            raise ValueError("unsupported plan review")
        values = raw["items"]
        if not isinstance(values, list):
            raise ValueError("invalid plan review items")
        return cls(
            status=PlanReviewStatus(raw["status"]),
            agent_summary=_bounded_optional_text(
                raw["agent_summary"],
                max_bytes=MAX_REVIEW_SUMMARY_BYTES,
            ),
            items=tuple(PlanReviewItem.from_dict(item) for item in values),
        )


def normalize_plan_review(
    value: object,
    *,
    candidate_ids: tuple[CandidateId, ...],
    mapped_ids: frozenset[CandidateId],
    verified_conflicts: Iterable[
        tuple[CandidateId, int, int]
    ] = (),
) -> PlanReview:
    """Accept safe Agent conclusions; silently discard invalid review parts."""

    available = set(candidate_ids) - set(mapped_ids)
    conflicts = set(verified_conflicts)
    valid_shape = (
        isinstance(value, dict)
        and frozenset(value)
        == {"summary", "unmapped_explanations"}
    )
    try:
        summary = (
            _bounded_optional_text(
                value["summary"],
                max_bytes=MAX_REVIEW_SUMMARY_BYTES,
            )
            if valid_shape
            else None
        )
    except ValueError:
        summary = None
    raw_items = (
        value["unmapped_explanations"]
        if valid_shape
        and isinstance(value["unmapped_explanations"], list)
        else []
    )
    items: list[PlanReviewItem] = []
    seen: set[CandidateId] = set()
    for raw in raw_items[:MAX_REVIEW_ITEMS]:
        if not isinstance(raw, dict):
            continue
        try:
            fields = check_fields(
                raw,
                frozenset(
                    {
                        "candidate_id",
                        "detail",
                        "episode",
                        "reason",
                        "related_video_id",
                        "season",
                    }
                ),
                field="unmapped_explanation",
            )
            candidate_id = CandidateId.parse(fields["candidate_id"])
            if candidate_id not in available or candidate_id in seen:
                continue
            reason = PlanReviewReason(fields["reason"])
            detail = _bounded_optional_text(
                fields["detail"],
                max_bytes=MAX_REVIEW_DETAIL_BYTES,
            )
            season = fields["season"]
            episode = fields["episode"]
            related_raw = fields["related_video_id"]
            related = (
                None
                if related_raw is None
                else CandidateId.parse(related_raw)
            )
            if related is not None and related not in available:
                related = None
            verification = PlanReviewVerification.ADVISORY
            if reason is PlanReviewReason.EXISTING_EPISODE:
                if (
                    type(season) is not int
                    or type(episode) is not int
                ):
                    continue
                if (candidate_id, season, episode) in conflicts:
                    verification = PlanReviewVerification.VERIFIED
            item = PlanReviewItem(
                candidate_id=candidate_id,
                reason=reason,
                verification=verification,
                agent_detail=detail,
                season=season,
                episode=episode,
                related_video_id=related,
            )
        except (DomainError, TypeError, ValueError):
            continue
        items.append(item)
        seen.add(candidate_id)
    agent_contributed = bool(summary is not None or items)
    conflict_groups: dict[CandidateId, set[tuple[int, int]]] = {}
    for candidate_id, season, episode in conflicts:
        if (
            candidate_id in available
            and type(season) is int
            and type(episode) is int
            and 0 <= season <= 999
            and 1 <= episode <= 100_000
        ):
            conflict_groups.setdefault(candidate_id, set()).add(
                (season, episode)
            )
    for candidate_id, locations in sorted(
        conflict_groups.items(),
        key=lambda item: item[0].ordinal,
    ):
        if len(locations) != 1:
            continue
        season, episode = next(iter(locations))
        existing_index = next(
            (
                index
                for index, item in enumerate(items)
                if item.candidate_id == candidate_id
            ),
            None,
        )
        existing = (
            None if existing_index is None else items[existing_index]
        )
        evidence = PlanReviewItem(
            candidate_id=candidate_id,
            reason=PlanReviewReason.EXISTING_EPISODE,
            verification=PlanReviewVerification.VERIFIED,
            agent_detail=(
                None if existing is None else existing.agent_detail
            ),
            season=season,
            episode=episode,
            related_video_id=(
                None if existing is None else existing.related_video_id
            ),
        )
        if existing_index is None:
            if len(items) >= MAX_REVIEW_ITEMS:
                advisory_index = next(
                    (
                        index
                        for index in range(len(items) - 1, -1, -1)
                        if items[index].verification
                        is not PlanReviewVerification.VERIFIED
                    ),
                    None,
                )
                if advisory_index is None:
                    continue
                items.pop(advisory_index)
            items.append(evidence)
        else:
            items[existing_index] = evidence
    status = (
        PlanReviewStatus.AGENT_AND_SYSTEM
        if agent_contributed
        else PlanReviewStatus.SYSTEM_ONLY
    )
    try:
        return PlanReview(
            status=status,
            agent_summary=summary,
            items=tuple(items),
        )
    except ValueError:
        return PlanReview.system_only(
            items=tuple(
                PlanReviewItem(
                    candidate_id=item.candidate_id,
                    reason=PlanReviewReason.EXISTING_EPISODE,
                    verification=PlanReviewVerification.VERIFIED,
                    season=item.season,
                    episode=item.episode,
                )
                for item in items
                if item.verification
                is PlanReviewVerification.VERIFIED
            )
        )


def merge_plan_reviews(
    stored: PlanReview | None,
    system: PlanReview,
) -> PlanReview:
    """Merge exact system evidence without inventing Agent intent."""

    if system.status not in {
        PlanReviewStatus.SYSTEM_ONLY,
        PlanReviewStatus.UNAVAILABLE,
    }:
        raise ValueError("invalid system review")
    if stored is None:
        return system
    merged = {item.candidate_id: item for item in stored.items}
    for evidence in system.items:
        existing = merged.get(evidence.candidate_id)
        merged[evidence.candidate_id] = PlanReviewItem(
            candidate_id=evidence.candidate_id,
            reason=evidence.reason,
            verification=PlanReviewVerification.VERIFIED,
            agent_detail=(
                None if existing is None else existing.agent_detail
            ),
            season=evidence.season,
            episode=evidence.episode,
            related_video_id=(
                None if existing is None else existing.related_video_id
            ),
        )
    has_agent = (
        stored.status is PlanReviewStatus.AGENT_AND_SYSTEM
        and (
            stored.agent_summary is not None
            or any(
                item.agent_detail is not None
                or item.verification is PlanReviewVerification.ADVISORY
                for item in stored.items
            )
        )
    )
    items = tuple(
        sorted(merged.values(), key=lambda item: item.candidate_id.ordinal)
    )
    if has_agent:
        return PlanReview(
            status=PlanReviewStatus.AGENT_AND_SYSTEM,
            agent_summary=stored.agent_summary,
            items=items,
        )
    if items:
        return PlanReview.system_only(items=items)
    return stored
