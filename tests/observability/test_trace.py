from __future__ import annotations

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.subtitle_acquisition import (
    EmbeddedChineseStatus,
    EmbeddedSubtitleInspection,
    EmbeddedSubtitleProbeStatus,
    SubtitleSelection,
    SubtitleSelectionDecision,
    SubtitleArchiveSetId,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.observability.trace import _attributes, build_trace
from reeloom.runtime.events import (
    CandidateSnapshotCreated,
    EmbeddedSubtitlesInspected,
    SubtitleSelectionSubmitted,
    RunStarted,
    ToolRejected,
    ToolRequested,
)
from reeloom.runtime.store import StoredEvent


def test_trace_redacts_unknown_model_controlled_tokens() -> None:
    secret = "sk-secret-model-controlled"
    events = (
        StoredEvent(1, RunStarted("run-trace", TmdbWorkType.ANIME)),
        StoredEvent(2, CandidateSnapshotCreated("snapshot:1", 0)),
        StoredEvent(3, ToolRequested("call-1", secret)),
        StoredEvent(
            4,
            ToolRejected("call-1", secret, secret, retryable=False),
        ),
    )

    trace = build_trace(events)
    content = trace.canonical_bytes()

    assert secret.encode() not in content
    assert b'"tool_name":"unknown"' in content
    assert b'"code":"other"' in content
    assert trace.summary.tool_rejections == 1


def test_embedded_probe_trace_contains_only_bounded_status_metadata() -> None:
    event = EmbeddedSubtitlesInspected(
        "call-probe",
        EmbeddedSubtitleInspection(
            CandidateId(CandidateKind.VIDEO, 7),
            2,
            EmbeddedSubtitleProbeStatus.INDETERMINATE,
            EmbeddedChineseStatus.UNKNOWN,
            (),
        ),
    )

    attributes = _attributes(event)

    assert attributes == {
        "chinese_status": "unknown",
        "probe_status": "indeterminate",
        "season_number": 2,
        "track_count": 0,
    }
    assert "video_id" not in attributes


def test_subtitle_selection_trace_omits_forum_and_archive_identities() -> None:
    event = SubtitleSelectionSubmitted(
        "call-select",
        SubtitleSelectionDecision.selected(
            (SubtitleSelection(2, SubtitleArchiveSetId(9)),)
        ),
    )

    assert _attributes(event) == {
        "selection_count": 1,
        "status": "selected",
    }
