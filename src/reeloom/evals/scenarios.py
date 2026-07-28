from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from reeloom.adapters.filesystem import (
    FilesystemPlanCompiler,
    FilesystemScanner,
)
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.agents.organizer import OrganizerContext, create_organizer_context
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.specials import SpecialKind
from reeloom.kernel.tmdb import (
    TmdbEpisode,
    TmdbLanguage,
    TmdbMovieDetails,
    TmdbSearchCandidate,
    TmdbSeasonDetails,
    TmdbSeriesDetails,
    TmdbWorkType,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.archive_directory import (
    FilesystemArchiveDirectoryBrowser,
)
from reeloom.ports.subtitles import SubtitleSample
from reeloom.runtime.budget import RunBudget
from reeloom.tools.candidates import SnapshotCandidateSource

BASELINE_MAPPING_SCENARIO = "baseline_mapping_v1"


class _BaselineTmdb:
    async def search_titles(
        self,
        *,
        query: str,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
        limit: int,
        include_adult: bool = True,
    ) -> tuple[TmdbSearchCandidate, ...]:
        del query, language, limit
        if work_type is not TmdbWorkType.ANIME or include_adult is not True:
            raise AssertionError("unexpected eval search")
        return (
            TmdbSearchCandidate(
                tmdb_id=200,
                localized_name="正确动画",
                original_name="Correct Anime",
                year=2024,
                original_language="ja",
                work_type=work_type,
            ),
        )

    async def get_movie(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
    ) -> TmdbMovieDetails:
        del tmdb_id, work_type, language
        raise AssertionError("movie lookup is outside this eval scenario")

    async def get_series(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
    ) -> TmdbSeriesDetails:
        if tmdb_id != 200 or work_type is not TmdbWorkType.ANIME:
            raise AssertionError("unexpected eval series")
        return TmdbSeriesDetails(
            tmdb_id=tmdb_id,
            language=language,
            localized_name=(
                "正确动画"
                if language is TmdbLanguage.ZH_CN
                else "Correct Anime"
            ),
            original_name="Correct Anime",
            first_air_year=2024,
            seasons=(),
            work_type=work_type,
        )

    async def get_season(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        season_number: int,
        language: TmdbLanguage,
    ) -> TmdbSeasonDetails:
        if (
            tmdb_id != 200
            or work_type is not TmdbWorkType.ANIME
            or season_number != 1
        ):
            raise AssertionError("unexpected eval season")
        return TmdbSeasonDetails(
            tmdb_id=tmdb_id,
            language=language,
            season_number=season_number,
            episodes=(
                TmdbEpisode(
                    season_number=1,
                    episode_number=1,
                    name="第一话",
                    overview="",
                    special_kind=SpecialKind.UNKNOWN,
                ),
                TmdbEpisode(
                    season_number=1,
                    episode_number=2,
                    name="第二话",
                    overview="",
                    special_kind=SpecialKind.UNKNOWN,
                ),
            ),
            work_type=work_type,
        )


class _BaselineSubtitleProvider:
    def __init__(self, source: SnapshotCandidateSource) -> None:
        self.snapshot_id = source.snapshot_id
        self.candidate_count = source.candidate_count

    async def sample(
        self,
        subtitle_id: CandidateId,
        *,
        max_bytes: int,
    ) -> SubtitleSample:
        if (
            subtitle_id != CandidateId(CandidateKind.SUBTITLE, 1)
            or max_bytes != 64 * 1024
        ):
            raise AssertionError("unexpected eval subtitle sample")
        return SubtitleSample(
            display_name="Correct Anime S01E02.chs.ass",
            content=b"untrusted subtitle text",
        )


def build_eval_scenario(
    scenario: str,
    *,
    workspace: Path,
    run_id: str,
    work_type: TmdbWorkType,
) -> OrganizerContext:
    if (
        scenario != BASELINE_MAPPING_SCENARIO
        or work_type is not TmdbWorkType.ANIME
    ):
        raise ValueError("unknown eval scenario")
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        raise ValueError("eval workspace must be absolute")
    workspace.mkdir(mode=0o700)
    source_path = workspace / "incoming"
    output_path = workspace / "output"
    plan_path = workspace / "plans"
    source_path.mkdir()
    output_path.mkdir()
    plan_path.mkdir()
    (source_path / "Correct Anime S01E02.mkv").write_bytes(b"video")
    (source_path / "Correct Anime S01E02.chs.ass").write_bytes(
        b"subtitle"
    )
    (source_path / "zz-unmapped.mkv").write_bytes(b"unmapped")

    scan = FilesystemScanner().scan(AuthorizedRoot.create(source_path))
    source = SnapshotCandidateSource.from_scanned(scan.snapshot)
    return create_organizer_context(
        run_id=run_id,
        candidate_source=source,
        work_type=work_type,
        tmdb_provider=_BaselineTmdb(),
        archive_browser=FilesystemArchiveDirectoryBrowser(
            run_id=run_id,
            root=AuthorizedRoot.create(output_path),
        ),
        subtitle_provider=_BaselineSubtitleProvider(source),
        plan_compiler=FilesystemPlanCompiler(
            scan=scan,
            output_root=AuthorizedRoot.create(output_path),
        ),
        plan_store=FilesystemPlanStore(
            AuthorizedRoot.create(plan_path)
        ),
        clock=lambda: datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
        budget=RunBudget(max_model_turns=32),
    )
