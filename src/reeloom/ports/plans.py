from __future__ import annotations

from datetime import datetime
from typing import Protocol

from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.mapping import MappingDraft
from reeloom.kernel.movie import MovieMappingDraft
from reeloom.kernel.movie_plan import MovieRenamePlan
from reeloom.kernel.movie_forward_execution import MovieRenamePlanV2
from reeloom.kernel.naming import (
    MovieIdentity,
    SeriesIdentity,
    SubtitleVariant,
)
from reeloom.kernel.initial_plan import InitialPlan
from reeloom.kernel.forward_execution import RenamePlanV2
from reeloom.kernel.rename_plan import RenamePlan, RootBinding
from reeloom.kernel.semantic_identity import SemanticRootBinding
from reeloom.kernel.tmdb import TmdbWorkType


class PlanCompiler(Protocol):
    """Run-scoped, read-only capability for one source/output root pair."""

    @property
    def snapshot_id(self) -> str: ...

    @property
    def candidate_count(self) -> int: ...

    @property
    def source_root_binding(self) -> RootBinding | SemanticRootBinding: ...

    @property
    def output_root_binding(self) -> RootBinding | SemanticRootBinding: ...

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
    ) -> RenamePlan | RenamePlanV2: ...

    def compile_movie(
        self,
        *,
        run_id: str,
        movie: MovieIdentity,
        mapping: MovieMappingDraft,
        subtitle_variants: tuple[
            tuple[CandidateId, SubtitleVariant],
            ...,
        ],
        created_at: datetime,
    ) -> MovieRenamePlan | MovieRenamePlanV2: ...


class PlanStore(Protocol):
    """Content-addressed persistence; callers load plans only by hash."""

    def save(self, plan: InitialPlan) -> None: ...

    def load(self, plan_hash: str) -> bytes: ...
