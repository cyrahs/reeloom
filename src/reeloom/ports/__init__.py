"""Provider-neutral interfaces implemented by external adapters."""

from reeloom.ports.approvals import ApprovalStore
from reeloom.ports.journals import JournalStore
from reeloom.ports.plans import PlanCompiler, PlanStore
from reeloom.ports.forward_filesystem import (
    ForwardFilesystem,
    ForwardMoveDiagnostic,
    ForwardMoveEffect,
)
from reeloom.ports.subtitles import (
    SubtitleSample,
    SubtitleSampleProvider,
)
from reeloom.ports.subtitle_acquisition import (
    DownloadedArchiveVolume,
    DownloadedSubtitleArchiveSet,
    InspectedSubtitleArchiveSet,
    SubtitleArchiveError,
    SubtitleArchiveErrorCode,
    SubtitleArchiveCache,
    SubtitleArchiveFetcher,
    SubtitleArchiveInspector,
    SubtitleAcquisitionPlanStore,
    SubtitleSearchProvider,
    SubtitleSearchProviderError,
    SubtitleSearchRequest,
    SubtitleSearchResult,
    SubtitleSearchErrorCode,
    VideoSubtitleInspector,
)
from reeloom.ports.tmdb import (
    TmdbErrorCode,
    TmdbProvider,
    TmdbProviderError,
)

__all__ = [
    "ApprovalStore",
    "JournalStore",
    "SubtitleSample",
    "SubtitleSampleProvider",
    "SubtitleAcquisitionPlanStore",
    "DownloadedArchiveVolume",
    "DownloadedSubtitleArchiveSet",
    "InspectedSubtitleArchiveSet",
    "SubtitleArchiveError",
    "SubtitleArchiveErrorCode",
    "SubtitleArchiveCache",
    "SubtitleArchiveFetcher",
    "SubtitleArchiveInspector",
    "SubtitleSearchProvider",
    "SubtitleSearchProviderError",
    "SubtitleSearchRequest",
    "SubtitleSearchResult",
    "SubtitleSearchErrorCode",
    "VideoSubtitleInspector",
    "PlanCompiler",
    "PlanStore",
    "ForwardFilesystem",
    "ForwardMoveDiagnostic",
    "ForwardMoveEffect",
    "TmdbErrorCode",
    "TmdbProvider",
    "TmdbProviderError",
]
