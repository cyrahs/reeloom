from __future__ import annotations

from datetime import datetime
from typing import Protocol

from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.mapping import MappingDraft
from reeloom.kernel.naming import SeriesIdentity, SubtitleVariant
from reeloom.kernel.rename_plan import RenamePlan, RootBinding
from reeloom.kernel.tmdb import TmdbWorkType


class PlanCompiler(Protocol):
    """Run-scoped, read-only capability for one source/output root pair."""

    @property
    def snapshot_id(self) -> str: ...

    @property
    def candidate_count(self) -> int: ...

    @property
    def source_root_binding(self) -> RootBinding: ...

    @property
    def output_root_binding(self) -> RootBinding: ...

    def compile(
        self,
        *,
        run_id: str,
        work_type: TmdbWorkType,
        series: SeriesIdentity,
        mapping: MappingDraft,
        subtitle_variants: tuple[
            tuple[CandidateId, SubtitleVariant],
            ...,
        ],
        created_at: datetime,
    ) -> RenamePlan: ...


class PlanStore(Protocol):
    """Content-addressed persistence; callers load plans only by hash."""

    def save(self, plan: RenamePlan) -> None: ...

    def load(self, plan_hash: str) -> bytes: ...
