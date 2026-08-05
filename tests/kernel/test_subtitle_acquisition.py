from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.rename_plan import RootBinding
from reeloom.kernel.subtitle_acquisition import (
    MAX_ARCHIVE_VOLUME_BYTES,
    MAX_COMPRESSION_RATIO,
    EmbeddedChineseStatus,
    EmbeddedSubtitleCodec,
    EmbeddedSubtitleInspection,
    EmbeddedSubtitleLanguage,
    EmbeddedSubtitleProbeStatus,
    EmbeddedSubtitleTrack,
    EmbeddedSubtitleTrackId,
    InspectedSubtitleMember,
    RejectedArchiveEntry,
    RejectedArchiveEntryReason,
    SubtitleAcquisitionPlan,
    SubtitleArchiveFormat,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleArchiveSetSummary,
    SubtitleArchiveSource,
    SubtitleArchiveVolume,
    SubtitleReleaseId,
    SubtitleReleaseSummary,
    SubtitleSearchDiagnostics,
    SubtitleSearchCursorId,
    SubtitleSearchEmptyStage,
    SubtitleSearchPage,
    SubtitleSelection,
    SubtitleSelectionDecision,
    SubtitleSelectionStatus,
    verify_subtitle_acquisition_plan_bytes,
)
from reeloom.ports.subtitle_acquisition import (
    SubtitleSearchRequest,
    SubtitleSearchResult,
)

_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _volume(
    *,
    index: int = 1,
    attachment_id: int = 101,
    size_bytes: int = 4_096,
    digest: str = "a" * 64,
) -> SubtitleArchiveVolume:
    return SubtitleArchiveVolume(
        index=index,
        attachment_id=attachment_id,
        size_bytes=size_bytes,
        sha256=digest,
    )


def _archive(
    *,
    archive_set_ordinal: int = 1,
    seasons: tuple[int, ...] = (1,),
    volumes: tuple[SubtitleArchiveVolume, ...] | None = None,
    format: SubtitleArchiveFormat = SubtitleArchiveFormat.RAR,
) -> SubtitleArchiveSource:
    return SubtitleArchiveSource(
        release_id=SubtitleReleaseId(archive_set_ordinal),
        archive_set_id=SubtitleArchiveSetId(archive_set_ordinal),
        format=format,
        season_numbers=seasons,
        thread_id=200 + archive_set_ordinal,
        post_id=300 + archive_set_ordinal,
        manifest_digest="b" * 64,
        volumes=volumes or (_volume(),),
    )


def _member(
    *,
    archive_set_ordinal: int = 1,
    source_path: str = "Subs/Show.S01E01.chs.ass",
    size_bytes: int = 1_024,
    digest: str = "c" * 64,
) -> InspectedSubtitleMember:
    return InspectedSubtitleMember(
        archive_set_id=SubtitleArchiveSetId(archive_set_ordinal),
        source_path=PurePosixPath(source_path),
        size_bytes=size_bytes,
        sha256=digest,
    )


def _plan(
    *,
    archives: tuple[SubtitleArchiveSource, ...] | None = None,
    members: tuple[InspectedSubtitleMember, ...] | None = None,
) -> SubtitleAcquisitionPlan:
    return SubtitleAcquisitionPlan.create(
        run_id="run-m13",
        config_revision_id="config-revision-7",
        created_at=_NOW,
        source_root=RootBinding(PurePosixPath("/watch"), 1, 2),
        source_folder="release",
        source_folder_device=1,
        source_folder_inode=3,
        folder_generation_id="folder-generation-9",
        candidate_snapshot_id="candidate-snapshot-v1:" + "d" * 64,
        tmdb_id=777,
        archives=archives or (_archive(),),
        inspected_members=members or (_member(),),
        rejected_entries=(
            RejectedArchiveEntry(
                archive_set_id=SubtitleArchiveSetId(1),
                member_name_digest="e" * 64,
                reason=RejectedArchiveEntryReason.UNSUPPORTED_TYPE,
            ),
        ),
    )


def test_embedded_inspection_is_strict_and_semantically_consistent() -> None:
    track = EmbeddedSubtitleTrack(
        track_id=EmbeddedSubtitleTrackId(1),
        codec=EmbeddedSubtitleCodec.ASS,
        language=EmbeddedSubtitleLanguage.ZH_HANS,
        default=True,
        forced=False,
    )
    inspection = EmbeddedSubtitleInspection(
        video_id=CandidateId(CandidateKind.VIDEO, 1),
        season_number=1,
        probe_status=EmbeddedSubtitleProbeStatus.PRESENT,
        chinese_status=EmbeddedChineseStatus.PRESENT,
        tracks=(track,),
    )

    assert str(inspection.tracks[0].track_id) == "embedded-sub:1"

    with pytest.raises(DomainError) as raised:
        EmbeddedSubtitleInspection(
            video_id=inspection.video_id,
            season_number=1,
            probe_status=EmbeddedSubtitleProbeStatus.ABSENT,
            chinese_status=EmbeddedChineseStatus.ABSENT,
            tracks=(track,),
        )
    assert raised.value.code is ErrorCode.INVALID_EMBEDDED_SUBTITLE_DATA


def test_indeterminate_probe_never_claims_chinese_absence() -> None:
    with pytest.raises(DomainError) as raised:
        EmbeddedSubtitleInspection(
            video_id=CandidateId(CandidateKind.VIDEO, 1),
            season_number=1,
            probe_status=EmbeddedSubtitleProbeStatus.INDETERMINATE,
            chinese_status=EmbeddedChineseStatus.ABSENT,
            tracks=(),
        )

    assert raised.value.code is ErrorCode.INVALID_EMBEDDED_SUBTITLE_DATA


def test_search_models_keep_urls_out_and_enforce_archive_shape() -> None:
    archive = SubtitleArchiveSetSummary(
        archive_set_id=SubtitleArchiveSetId(1),
        format=SubtitleArchiveFormat.RAR,
        volume_count=2,
        declared_size=8_192,
    )
    release = SubtitleReleaseSummary(
        release_id=SubtitleReleaseId(1),
        archive_sets=(archive,),
        title="作品 第一季 简繁字幕",
        post_excerpt="匹配 BDRip，包含全季字幕",
        coverage_hint="S01E01-E12",
        language_hints=("简体", "繁体"),
        release_group_hints=("ExampleGroup",),
        match_reasons=("title_alias", "season"),
        warnings=(),
        evidence_complete=True,
    )
    page = SubtitleSearchPage(
        items=(release,),
        next_cursor=SubtitleSearchCursorId(2),
        complete=False,
    )

    assert str(page.items[0].archive_sets[0].archive_set_id) == "subarchive:1"

    with pytest.raises(DomainError) as raised:
        SubtitleReleaseSummary(
            release_id=SubtitleReleaseId(2),
            archive_sets=(archive,),
            title="https://attacker.invalid/prompt",
            post_excerpt="text",
            coverage_hint="S01",
            language_hints=(),
            release_group_hints=(),
            match_reasons=(),
            warnings=(),
            evidence_complete=True,
        )
    assert raised.value.code is ErrorCode.INVALID_SUBTITLE_SEARCH_DATA

    with pytest.raises(DomainError):
        SubtitleArchiveSetSummary(
            archive_set_id=SubtitleArchiveSetId(2),
            format=SubtitleArchiveFormat.SEVEN_Z,
            volume_count=2,
            declared_size=8_192,
        )


def test_search_request_accepts_only_bounded_distinct_tmdb_aliases() -> None:
    request = SubtitleSearchRequest(
        title_aliases=("作品", "Original Title"),
        season_number=1,
        cursor=None,
        limit=10,
    )

    assert request.title_aliases == ("作品", "Original Title")

    with pytest.raises(DomainError):
        SubtitleSearchRequest(
            title_aliases=("作品", "作品"),
            season_number=1,
            cursor=None,
            limit=10,
        )


def test_search_result_binds_summary_to_stable_forum_capability() -> None:
    summary = SubtitleArchiveSetSummary(
        SubtitleArchiveSetId(1),
        SubtitleArchiveFormat.SEVEN_Z,
        1,
        4_096,
    )
    page = SubtitleSearchPage(
        (
            SubtitleReleaseSummary(
                SubtitleReleaseId(1),
                (summary,),
                "作品",
                "全季简繁字幕",
                "S01",
                ("zh-hans", "zh-hant"),
                ("ExampleGroup",),
                ("title_alias",),
                (),
                True,
            ),
        ),
        None,
        True,
    )
    capability = SubtitleArchiveSetCapability(
        SubtitleArchiveSetId(1),
        SubtitleReleaseId(1),
        SubtitleArchiveFormat.SEVEN_Z,
        10081,
        95257,
        (34768,),
        4_096,
    )

    diagnostics = SubtitleSearchDiagnostics(
        ("作品",),
        (1,),
        1,
        1,
        1,
        1,
        1,
        1,
        1,
    )

    assert SubtitleSearchResult(
        page,
        (capability,),
        diagnostics,
    ).capabilities == (
        capability,
    )

    with pytest.raises(DomainError):
        SubtitleSearchResult(
            page,
            (
                SubtitleArchiveSetCapability(
                    SubtitleArchiveSetId(1),
                    SubtitleReleaseId(1),
                    SubtitleArchiveFormat.SEVEN_Z,
                    10081,
                    95257,
                    (34768,),
                    4_095,
                ),
            ),
            diagnostics,
        )


def test_search_diagnostics_are_bounded_and_identify_empty_stage() -> None:
    diagnostics = SubtitleSearchDiagnostics(
        ("作品", "Original Title"),
        (0, 0),
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )

    assert diagnostics.empty_stage is SubtitleSearchEmptyStage.FORUM_SEARCH

    with pytest.raises(DomainError):
        SubtitleSearchDiagnostics(
            ("https://outside.invalid",),
            (0,),
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )

    with pytest.raises(DomainError):
        SubtitleSearchDiagnostics(
            ("作品",),
            (10_001,),
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )


def test_selection_requires_one_agent_decision_per_season() -> None:
    decision = SubtitleSelectionDecision.selected(
        (
            SubtitleSelection(2, SubtitleArchiveSetId(2)),
            SubtitleSelection(1, SubtitleArchiveSetId(1)),
        )
    )

    assert decision.status is SubtitleSelectionStatus.SELECTED
    assert tuple(item.season_number for item in decision.selections) == (1, 2)

    with pytest.raises(DomainError) as raised:
        SubtitleSelectionDecision.selected(
            (
                SubtitleSelection(1, SubtitleArchiveSetId(1)),
                SubtitleSelection(1, SubtitleArchiveSetId(2)),
            )
        )
    assert raised.value.code is ErrorCode.INVALID_SUBTITLE_SELECTION

    attention = SubtitleSelectionDecision.needs_attention(
        "subtitle_evidence_ambiguous"
    )
    assert attention.selections == ()


def test_acquisition_plan_is_canonical_immutable_and_round_trips() -> None:
    plan = _plan()

    restored = SubtitleAcquisitionPlan.from_canonical_bytes(
        plan.canonical_bytes(),
        plan_hash=plan.plan_hash,
    )

    assert restored == plan
    assert plan.verify_hash()
    assert verify_subtitle_acquisition_plan_bytes(
        plan.canonical_bytes(), plan.plan_hash
    )
    assert plan.destination_directory == PurePosixPath(
        "reeloom-acquired-" + plan.plan_hash.removeprefix("sha256:")
    )
    assert plan.members[0].destination_name == (
        "Show.S01E01.chs--a1-cccccccccccc.ass"
    )
    assert "forum.php" not in plan.canonical_bytes().decode("ascii")


def test_acquisition_plan_hash_binds_volume_and_member_content() -> None:
    original = _plan()
    changed_volume = _plan(
        archives=(
            _archive(
                volumes=(
                    _volume(digest="f" * 64),
                ),
            ),
        )
    )
    changed_member = _plan(
        members=(_member(digest="f" * 64),)
    )

    assert original.plan_hash != changed_volume.plan_hash
    assert original.plan_hash != changed_member.plan_hash


def test_semantic_destination_tamper_is_rejected_even_with_new_hash() -> None:
    plan = _plan()
    payload = json.loads(plan.canonical_bytes())
    payload["members"][0]["destination_name"] = "attacker-choice.ass"
    content = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    plan_hash = "sha256:" + hashlib.sha256(content).hexdigest()

    with pytest.raises(DomainError) as raised:
        SubtitleAcquisitionPlan.from_canonical_bytes(
            content,
            plan_hash=plan_hash,
        )
    assert raised.value.code is ErrorCode.INVALID_SUBTITLE_ACQUISITION_PLAN


def test_plan_rejects_duplicate_season_and_target_collision() -> None:
    with pytest.raises(DomainError):
        _plan(
            archives=(
                _archive(archive_set_ordinal=1, seasons=(1,)),
                _archive(archive_set_ordinal=2, seasons=(1,)),
            ),
            members=(
                _member(archive_set_ordinal=1),
                _member(
                    archive_set_ordinal=2,
                    source_path="Other/Show.S01E02.ass",
                    digest="f" * 64,
                ),
            ),
        )

    with pytest.raises(DomainError) as raised:
        _plan(
            members=(
                _member(source_path="one/same.ass"),
                _member(source_path="two/same.ass"),
            )
        )
    assert raised.value.code is ErrorCode.SUBTITLE_ACQUISITION_COLLISION


@pytest.mark.parametrize(
    "source_path",
    (
        "../escape.ass",
        "/absolute.ass",
        "C:/windows-drive.ass",
        ".env.ass",
        "nested/archive.zip",
    ),
)
def test_plan_rejects_unsafe_or_non_subtitle_members(source_path: str) -> None:
    with pytest.raises(DomainError):
        _member(source_path=source_path)


def test_plan_rejects_archive_and_expansion_limits() -> None:
    with pytest.raises(DomainError):
        _volume(size_bytes=MAX_ARCHIVE_VOLUME_BYTES + 1)

    with pytest.raises(DomainError):
        _plan(
            archives=(
                _archive(
                    volumes=(_volume(size_bytes=1),),
                ),
            ),
            members=(
                _member(
                    size_bytes=MAX_COMPRESSION_RATIO + 1,
                ),
            ),
        )
