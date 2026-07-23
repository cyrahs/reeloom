from __future__ import annotations

import asyncio
from pathlib import Path

from reeloom.adapters.filesystem import FilesystemScanner
from reeloom.agents.organizer import (
    create_organizer_context,
    run_episode_organizer,
)
from reeloom.agents.scripted_model import (
    FinalStep,
    ScriptedModel,
    ToolCallStep,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.state import StopReason
from reeloom.tools.candidates import SnapshotCandidateSource


def test_fake_agent_can_page_a_real_safe_snapshot(tmp_path: Path) -> None:
    root_path = tmp_path / "media"
    root_path.mkdir()
    (root_path / "Episode 01.mkv").write_bytes(b"video")
    scan = FilesystemScanner().scan(AuthorizedRoot.create(root_path))
    source = SnapshotCandidateSource.from_scanned(scan.snapshot)
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=source,
    )
    model = ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={
                    "kind": "video",
                    "cursor": 0,
                    "limit": 10,
                },
                call_id="call-1",
            ),
            FinalStep(
                text="One video candidate is available.",
                expect_input_contains="Episode 01.mkv",
            ),
        )
    )

    result = asyncio.run(
        run_episode_organizer(
            context=context,
            model=model,
            prompt="Inspect the video candidates.",
        )
    )

    assert result.state.candidate_snapshot_id == scan.snapshot.snapshot_id
    assert result.state.candidate_count == 1
    assert result.state.stop_reason is StopReason.MODEL_FINAL
