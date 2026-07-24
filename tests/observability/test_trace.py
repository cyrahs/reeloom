from __future__ import annotations

from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.observability.trace import build_trace
from reeloom.runtime.events import (
    CandidateSnapshotCreated,
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
