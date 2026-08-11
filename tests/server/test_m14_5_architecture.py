from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]


def test_active_subtitle_pipeline_has_no_persistent_stat_bridge() -> None:
    active_files = (
        "src/reeloom/server/agent_worker.py",
        "src/reeloom/server/subtitle_acquisition.py",
        "src/reeloom/server/subtitle_acquisition_service.py",
    )
    forbidden = (
        "legacy_source_root_binding",
        "source_folder_device",
        "source_folder_inode",
        "SubtitleAcquisitionPlanV1",
    )

    for relative in active_files:
        source = (_ROOT / relative).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"{relative} reintroduced {token}"


def test_production_composition_uses_shared_subtitle_operation_ledger() -> None:
    source = (_ROOT / "src/reeloom/server/composition.py").read_text(
        encoding="utf-8"
    )

    assert "operation_approvals=approvals" in source
    assert "operations=forward_operations" in source
    assert "subtitle_plan_sink=subtitle_acquisitions.register_plan" in source
    for forbidden in (
        "FilesystemApprovalStore",
        "FilesystemExecutor",
        "FilesystemJournalStore",
        "FilesystemFolderJournalStore",
        "FolderDispositionCoordinator",
        "FolderDispositionExecutor",
        "PostgresSubtitleSuccessorOutbox",
        "PostgresSubtitlePublicationRepository",
        "SubtitleSuccessorWorker",
        "SubtitleScanWorker",
        "subtitle_successors=",
        "subtitle_scans=",
    ):
        assert forbidden not in source


def test_active_run_registration_does_not_consume_legacy_subtitle_outbox() -> None:
    source = (
        _ROOT / "src/reeloom/server/scheduler_repository.py"
    ).read_text(encoding="utf-8")
    register_run = source.split(
        "def _register_run_in_transaction(", 1
    )[1].split(
        "\n\nclass PostgresSchedulerRepository", 1
    )[0]

    assert "generation_requests_v2" in register_run
    assert "successor_discovery_id" in register_run
    assert "subtitle_scan_requests_v2" not in register_run


def test_legacy_effect_graph_is_opt_in_only_for_component_history() -> None:
    for relative in (
        "src/reeloom/server/api.py",
        "src/reeloom/server/background.py",
    ):
        source = (_ROOT / relative).read_text(encoding="utf-8")
        assert "legacy_effects_enabled: bool = False" in source
        assert "legacy_effects_enabled: bool = True" not in source
