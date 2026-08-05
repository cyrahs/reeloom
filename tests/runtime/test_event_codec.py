from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.archive_directory import (
    ArchiveDirectoryCapability,
    ArchiveDirectoryListing,
    ArchiveSearchRecord,
)
from reeloom.kernel.candidates import Candidate, CandidateSnapshot
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.movie import MovieMappingDraft
from reeloom.kernel.plan_review import PlanReview
from reeloom.kernel.naming import (
    MovieIdentity,
    SeriesIdentity,
    SubtitleVariant,
)
from reeloom.kernel.rename_plan import (
    RenamePlan,
    RootBinding,
    compile_plan_draft,
)
from reeloom.kernel.subtitle_acquisition import (
    EmbeddedChineseStatus,
    EmbeddedSubtitleCodec,
    EmbeddedSubtitleInspection,
    EmbeddedSubtitleLanguage,
    EmbeddedSubtitleProbeStatus,
    EmbeddedSubtitleTrack,
    EmbeddedSubtitleTrackId,
    SubtitleArchiveFormat,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleArchiveSetSummary,
    SubtitleReleaseId,
    SubtitleReleaseSummary,
    SubtitleSearchDiagnostics,
    SubtitleSearchPage,
    SubtitleSearchRecord,
    SubtitleSelection,
    SubtitleSelectionDecision,
)
from reeloom.kernel.scanner import ScannedFile, build_candidate_snapshot
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType
from reeloom.runtime.errors import RuntimeDomainError
from reeloom.runtime.event_codec import decode_event, encode_event
from reeloom.runtime.events import (
    ApplyFailed,
    ApplyStarted,
    ArchiveDirectoryListed,
    ArchiveSearchObserved,
    ApprovalRequested,
    CandidateSnapshotCreated,
    SubtitleAcquisitionConfigured,
    ExistingInventoryObserved,
    EmbeddedSubtitlesInspected,
    SubtitleSearchObserved,
    SubtitleSearchFailed,
    SubtitleSelectionSubmitted,
    ExecutionSettled,
    InteractionCompleted,
    MappingRejected,
    MappingReviewCaptured,
    MappingSubmitted,
    MovieMappingSubmitted,
    MovieSelected,
    ModelUsageRecorded,
    MoveApplied,
    PlanApproved,
    PlanBuilt,
    RollbackCompleted,
    RunCompleted,
    RunFailed,
    RunStarted,
    RunStopped,
    SeriesSelected,
    SubtitleVariantDetected,
    TmdbCandidatesObserved,
    TmdbSeasonCatalogObserved,
    ToolRejected,
    ToolRequested,
    ToolSucceeded,
)
from reeloom.runtime.state import MappingValidationIssue, StopReason


def _mapping_and_plan() -> tuple[MappingDraft, RenamePlan]:
    snapshot = build_candidate_snapshot(
        (
            ScannedFile(
                relative_path=PurePosixPath("episode.mkv"),
                kind=CandidateKind.VIDEO,
                size_bytes=10,
                device=1,
                inode=2,
                mtime_ns=3,
                ctime_ns=4,
            ),
        )
    )
    mapping = MappingDraft.from_dict(
        {
            "videos": [
                {
                    "video_id": "video:1",
                    "season": 1,
                    "episode_start": 1,
                    "episode_end": 1,
                }
            ],
            "subtitles": [],
        },
        candidates=snapshot.candidates,
        catalog=EpisodeCatalog.from_counts({1: 12}),
    )
    series = SeriesIdentity("测试动画", 2025, 42)
    draft = compile_plan_draft(
        series=series,
        mapping=mapping,
        candidates=snapshot,
        subtitle_variants=(),
    )
    plan = RenamePlan.create(
        run_id="run-m7",
        work_type=TmdbWorkType.ANIME,
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        source_root=RootBinding(PurePosixPath("/source"), 1, 10),
        output_root=RootBinding(PurePosixPath("/output"), 1, 11),
        candidate_snapshot=snapshot,
        subtitle_variants=(),
        draft=draft,
        checked_destinations=(draft.moves[0].destination,),
    )
    return mapping, plan


def _event_samples() -> tuple[object, ...]:
    mapping, plan = _mapping_and_plan()
    movie_candidates = CandidateSnapshot.create(
        (
            Candidate(
                CandidateId(CandidateKind.VIDEO, 1),
                CandidateKind.VIDEO,
                "video:1",
            ),
            Candidate(
                CandidateId(CandidateKind.SUBTITLE, 1),
                CandidateKind.SUBTITLE,
                "subtitle:1",
            ),
        )
    )
    movie_mapping = MovieMappingDraft.from_dict(
        {
            "video_id": "video:1",
            "subtitle_ids": ["subtitle:1"],
        },
        candidates=movie_candidates,
    )
    source_root = RootBinding(PurePosixPath("/source"), 1, 10)
    output_root = RootBinding(PurePosixPath("/output"), 1, 11)
    series = SeriesIdentity("测试动画", 2025, 42)
    archive_capability = ArchiveDirectoryCapability(
        run_id="run-m7",
        directory_id="dir-1",
        parent_id=None,
        relative_path=PurePosixPath("旧项目"),
        name="旧项目",
        depth=1,
        device=1,
        inode=2,
        mtime_ns=3,
        ctime_ns=4,
    )
    return (
        RunStarted("run-m7", TmdbWorkType.ANIME),
        CandidateSnapshotCreated(
            "snapshot:1",
            1,
            (CandidateId(CandidateKind.VIDEO, 1),),
            source_root,
            output_root,
        ),
        TmdbCandidatesObserved(
            (TmdbCandidateRef(TmdbWorkType.ANIME, 42),)
        ),
        SeriesSelected(series, TmdbWorkType.ANIME, "/series.jpg"),
        MovieSelected(
            MovieIdentity("测试电影", 2025, 43),
            TmdbWorkType.MOVIE,
            "/movie.jpg",
        ),
        TmdbSeasonCatalogObserved(
            "call-1", 42, TmdbWorkType.ANIME, 1, 12
        ),
        ExistingInventoryObserved(
            "call-2", 42, TmdbWorkType.ANIME, ((1, 1),)
        ),
        ArchiveSearchObserved(
            ArchiveSearchRecord(
                call_id="archive-search",
                mode="name",
                query="旧项目",
                tmdb_id=42,
                work_type=TmdbWorkType.ANIME,
                directory_ids=("dir-1",),
                cursor=0,
                next_cursor=None,
                complete=True,
                observed_at=datetime(2026, 7, 28, tzinfo=UTC),
            ),
            (archive_capability,),
        ),
        ArchiveDirectoryListed(
            ArchiveDirectoryListing(
                call_id="archive-list",
                directory_id="dir-1",
                child_ids=(),
                videos=("旧项目 S01E01.mkv",),
                occupied=((1, 1),),
                cursor=0,
                next_cursor=None,
                complete=True,
                observed_at=datetime(2026, 7, 28, tzinfo=UTC),
            ),
            (),
        ),
        SubtitleVariantDetected(
            "call-3",
            CandidateId(CandidateKind.SUBTITLE, 1),
            SubtitleVariant.CHS,
        ),
        EmbeddedSubtitlesInspected(
            "call-probe",
            EmbeddedSubtitleInspection(
                CandidateId(CandidateKind.VIDEO, 1),
                1,
                EmbeddedSubtitleProbeStatus.PRESENT,
                EmbeddedChineseStatus.PRESENT,
                (
                    EmbeddedSubtitleTrack(
                        EmbeddedSubtitleTrackId(1),
                        EmbeddedSubtitleCodec.ASS,
                        EmbeddedSubtitleLanguage.ZH_HANS,
                        True,
                        False,
                    ),
                ),
            ),
        ),
        SubtitleAcquisitionConfigured(True),
        SubtitleSearchObserved(
            "call-search-sub",
            SubtitleSearchRecord(
                season_number=1,
                cursor=None,
                page=SubtitleSearchPage(
                    items=(
                        SubtitleReleaseSummary(
                            release_id=SubtitleReleaseId(1),
                            archive_sets=(
                                SubtitleArchiveSetSummary(
                                    SubtitleArchiveSetId(1),
                                    SubtitleArchiveFormat.SEVEN_Z,
                                    1,
                                    1024,
                                ),
                            ),
                            title="测试字幕",
                            post_excerpt="简短证据",
                            coverage_hint="S01",
                            language_hints=("简体中文",),
                            release_group_hints=("字幕组",),
                            match_reasons=("标题匹配",),
                            warnings=(),
                            evidence_complete=True,
                        ),
                    ),
                    next_cursor=None,
                    complete=True,
                ),
            ),
            (
                SubtitleArchiveSetCapability(
                    SubtitleArchiveSetId(1),
                    SubtitleReleaseId(1),
                    SubtitleArchiveFormat.SEVEN_Z,
                    10081,
                    95257,
                    (34768,),
                    1024,
                ),
            ),
            SubtitleSearchDiagnostics(
                ("测试动画",),
                (1,),
                1,
                1,
                1,
                1,
                1,
                1,
                1,
            ),
        ),
        SubtitleSearchFailed(
            "call-search-failed",
            1,
            "subtitle_search_unavailable",
        ),
        SubtitleSelectionSubmitted(
            "call-select-sub",
            SubtitleSelectionDecision.selected(
                (SubtitleSelection(1, SubtitleArchiveSetId(1)),)
            ),
        ),
        MappingRejected(
            "call-4",
            MappingValidationIssue(
                "invalid",
                (("candidate_ids", ("video:1",)),),
            ),
        ),
        MappingReviewCaptured(
            "call-5",
            PlanReview.system_only(),
        ),
        MappingSubmitted("call-5", "snapshot:1", mapping),
        MovieMappingSubmitted(
            "call-movie",
            "snapshot:movie",
            movie_mapping,
        ),
        PlanBuilt(plan),
        ApprovalRequested(plan.plan_hash),
        PlanApproved(plan.plan_hash, "approval:1"),
        ApplyStarted(plan.plan_hash, "approval:1"),
        MoveApplied(CandidateId(CandidateKind.VIDEO, 1)),
        ApplyFailed("preflight_failed"),
        RollbackCompleted("transaction:1", 1),
        RunCompleted("transaction:1", 1),
        ModelUsageRecorded(10, 2, 12),
        InteractionCompleted(
            interaction_id="interaction-1",
            kind="revision",
            model_turns=1,
            model_tokens=12,
            fresh_mapping_submitted=True,
            final_plan_hash=plan.plan_hash,
            plan_hash=plan.plan_hash,
        ),
        ExecutionSettled(
            plan_hash=plan.plan_hash,
            approval_id="approval:1",
            transaction_id="transaction:1",
            status="completed",
            applied_count=1,
            rolled_back_count=0,
        ),
        ToolRequested("call-6", "search_tmdb"),
        ToolSucceeded("call-6", "search_tmdb"),
        ToolRejected("call-7", "submit_mapping", "invalid", True),
        RunStopped(StopReason.AWAITING_APPROVAL),
        RunFailed("fatal"),
    )


@pytest.mark.parametrize("event", _event_samples())
def test_runtime_event_codec_round_trips_every_event(event: object) -> None:
    encoded = encode_event(event)  # type: ignore[arg-type]

    assert decode_event(encoded) == event
    assert encode_event(decode_event(encoded)) == encoded


def test_subtitle_search_event_codec_accepts_legacy_event_without_diagnostics() -> None:
    event = next(
        item for item in _event_samples() if isinstance(item, SubtitleSearchObserved)
    )
    payload = json.loads(encode_event(event))
    del payload["payload"]["diagnostics"]
    legacy = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    decoded = decode_event(legacy)

    assert isinstance(decoded, SubtitleSearchObserved)
    assert decoded.diagnostics is None


def test_event_codec_rejects_unknown_extra_and_noncanonical_data() -> None:
    encoded = encode_event(
        RunStarted("run-m7", TmdbWorkType.ANIME)
    )
    payload = json.loads(encoded)
    payload["payload"]["unexpected"] = True
    extra = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    for invalid in (
        extra,
        encoded + b"\n",
        encoded.replace(b"run_started", b"unknown_event"),
    ):
        with pytest.raises(RuntimeDomainError):
            decode_event(invalid)


def test_event_codec_rejects_extreme_mapping_before_expansion() -> None:
    mapping, _ = _mapping_and_plan()
    encoded = encode_event(
        MappingSubmitted("call-5", "snapshot:1", mapping)
    )
    payload = json.loads(encoded)
    payload["payload"]["mapping"]["videos"][0]["episode_end"] = 10**18
    extreme = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")

    with pytest.raises(RuntimeDomainError):
        decode_event(extreme)
