"""I/O implementations kept outside the deterministic kernel."""

from reeloom.adapters.approval import FilesystemApprovalStore
from reeloom.adapters.filesystem import (
    FfprobeProcessResult,
    FfprobeResultStatus,
    FilesystemPlanCompiler,
    FilesystemScanResult,
    FilesystemScanner,
    FilesystemSubtitleSampleProvider,
    FilesystemVideoSubtitleInspector,
    ScanLimits,
)
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.adapters.subtitle_plan_store import (
    FilesystemSubtitleAcquisitionPlanStore,
)
from reeloom.adapters.subtitle_archive import (
    FilesystemSubtitleArchiveInspector,
    FixedSevenZipRunner,
)
from reeloom.adapters.journal import FilesystemJournalStore
from reeloom.adapters.subtitle_journal import (
    FilesystemSubtitleAcquisitionJournalStore,
)
from reeloom.adapters.tmdb import TmdbHttpAdapter, TmdbHttpLimits
from reeloom.adapters.acgrip import (
    AcgripDiscuzParser,
    AcgripSubtitleArchiveFetcher,
    AcgripSubtitleSearchProvider,
)

__all__ = [
    "FilesystemApprovalStore",
    "FilesystemScanResult",
    "FilesystemPlanCompiler",
    "FilesystemScanner",
    "FilesystemSubtitleSampleProvider",
    "FfprobeProcessResult",
    "FfprobeResultStatus",
    "FilesystemVideoSubtitleInspector",
    "FilesystemPlanStore",
    "FilesystemSubtitleAcquisitionPlanStore",
    "FilesystemSubtitleArchiveInspector",
    "FixedSevenZipRunner",
    "FilesystemJournalStore",
    "FilesystemSubtitleAcquisitionJournalStore",
    "ScanLimits",
    "TmdbHttpAdapter",
    "TmdbHttpLimits",
    "AcgripDiscuzParser",
    "AcgripSubtitleArchiveFetcher",
    "AcgripSubtitleSearchProvider",
]
