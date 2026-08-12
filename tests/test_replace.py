from __future__ import annotations

from reeloom.adapters.ffprobe import Probe
from reeloom.models import (
    EpisodeSpan,
    FileKind,
    MediaIdentity,
    MediaType,
    Move,
    MoveKind,
    Plan,
    Root,
    SnapshotFile,
    SubtitleVariant,
)
from reeloom.replace import (
    ExistingFile,
    GroupVerdict,
    QualitySignal,
    ReplaceError,
    ReplacementDecision,
    ReplaceResolution,
    apply_decision,
    decide,
    resolve,
)
from reeloom.trash import TRASH_DIR

import pytest

SERIES = MediaIdentity(MediaType.ANIME, 123, "Show", 2024)
MOVIE = MediaIdentity(MediaType.MOVIE, 456, "Feature", 2016)
FOLDER = "Show (2024) {tmdb-123}"
RUN_ID = "run-1"


def probe(height: int, bit_rate: int = 0) -> Probe:
    return Probe(
        height=height,
        duration_seconds=1400.0,
        bit_rate=bit_rate or None,
        video_codec="hevc",
    )


def incoming_episode(
    episode: int, size: int, *, extension: str = ".mkv", season: int = 1
) -> tuple[SnapshotFile, Move]:
    candidate_id = f"V{episode}"
    name = f"[BD] Show - {episode:02d}{extension}"
    snapshot = SnapshotFile(candidate_id, name, FileKind.VIDEO, size)
    move = Move(
        kind=MoveKind.MEDIA,
        source_root=Root.INBOUND,
        source_path=f"Drop/{name}",
        dest_root=Root.LIBRARY,
        dest_path=(
            f"{FOLDER}/S{season:02d}/Show S{season:02d}E{episode:02d}{extension}"
        ),
        candidate_id=candidate_id,
    )
    return snapshot, move


def series_plan(*pairs: tuple[SnapshotFile, Move], moves_prefix: tuple[Move, ...] = ()):
    snapshot = tuple(pair[0] for pair in pairs)
    moves = moves_prefix + tuple(pair[1] for pair in pairs)
    return Plan(identity=SERIES, moves=moves), snapshot


def lib_file(
    episode: int, size: int, *, extension: str = ".mkv", season: int = 1
) -> ExistingFile:
    return ExistingFile(
        root=Root.LIBRARY,
        extra_base=None,
        relative_path=(
            f"{FOLDER}/S{season:02d}/Show S{season:02d}E{episode:02d}{extension}"
        ),
        size_bytes=size,
        span=EpisodeSpan(season, episode, episode),
    )


def extra_file(episode: int, size: int, *, base: str = "/data/anirss") -> ExistingFile:
    return ExistingFile(
        root=Root.EXTRA,
        extra_base=base,
        relative_path=f"Show/Season 1/Show S01E{episode:02d}.mp4",
        size_bytes=size,
        span=EpisodeSpan(1, episode, episode),
    )


# ---- the decision matrix -------------------------------------------------


def test_no_overlap_is_a_plain_import() -> None:
    plan, snapshot = series_plan(incoming_episode(1, 100))
    decision = decide(plan, snapshot, [])
    assert [group.verdict for group in decision.groups] == [GroupVerdict.IMPORT]
    assert decision.groups[0].reason == "no_overlap"
    assert decision.groups[0].new_episodes == (1,)
    assert not decision.needs_confirmation


def test_identical_files_are_discarded_with_confidence() -> None:
    plan, snapshot = series_plan(
        incoming_episode(1, 100), incoming_episode(2, 200)
    )
    decision = decide(plan, snapshot, [lib_file(1, 100), lib_file(2, 200)])
    group = decision.groups[0]
    assert group.verdict is GroupVerdict.DISCARD
    assert group.reason == "identical_files"
    assert not decision.needs_confirmation


def test_same_size_but_different_extension_is_not_identical() -> None:
    plan, snapshot = series_plan(incoming_episode(1, 100))
    decision = decide(plan, snapshot, [lib_file(1, 100, extension=".mp4")])
    assert decision.groups[0].reason != "identical_files"


def test_clear_size_upgrade_replaces_automatically() -> None:
    plan, snapshot = series_plan(incoming_episode(1, 300))
    decision = decide(plan, snapshot, [lib_file(1, 200)])
    group = decision.groups[0]
    assert group.verdict is GroupVerdict.REPLACE
    assert group.reason == "clear_upgrade"
    assert group.ratio == 1.5


def test_the_season_is_judged_as_a_whole() -> None:
    # One episode is smaller than the existing copy, but the batch overall
    # clears the bar — the whole season replaces.
    plan, snapshot = series_plan(
        incoming_episode(1, 90), incoming_episode(2, 400)
    )
    decision = decide(plan, snapshot, [lib_file(1, 100), lib_file(2, 200)])
    assert decision.groups[0].verdict is GroupVerdict.REPLACE


def test_gray_zone_with_better_quality_replaces() -> None:
    plan, snapshot = series_plan(incoming_episode(1, 110))
    existing = lib_file(1, 100)
    decision = decide(
        plan,
        snapshot,
        [existing],
        probes_incoming={"V1": probe(1080)},
        probes_existing={existing.key: probe(720)},
    )
    group = decision.groups[0]
    assert group.verdict is GroupVerdict.REPLACE
    assert group.reason == "quality_upgrade"
    assert group.quality is QualitySignal.BETTER


def test_gray_zone_without_quality_signal_goes_manual() -> None:
    plan, snapshot = series_plan(incoming_episode(1, 110))
    decision = decide(plan, snapshot, [lib_file(1, 100)])
    group = decision.groups[0]
    assert group.verdict is GroupVerdict.MANUAL
    assert group.reason == "ambiguous_upgrade"
    assert decision.needs_confirmation


def test_lower_resolution_goes_manual_despite_bigger_size() -> None:
    plan, snapshot = series_plan(incoming_episode(1, 400))
    existing = lib_file(1, 100)
    decision = decide(
        plan,
        snapshot,
        [existing],
        probes_incoming={"V1": probe(720)},
        probes_existing={existing.key: probe(1080)},
    )
    group = decision.groups[0]
    assert group.verdict is GroupVerdict.MANUAL
    assert group.reason == "quality_conflict"


def test_smaller_incoming_is_discarded() -> None:
    plan, snapshot = series_plan(incoming_episode(1, 80))
    decision = decide(plan, snapshot, [lib_file(1, 100)])
    group = decision.groups[0]
    assert group.verdict is GroupVerdict.DISCARD
    assert group.reason == "not_an_upgrade"
    assert not decision.needs_confirmation


def test_smaller_but_higher_resolution_goes_manual() -> None:
    plan, snapshot = series_plan(incoming_episode(1, 80))
    existing = lib_file(1, 100)
    decision = decide(
        plan,
        snapshot,
        [existing],
        probes_incoming={"V1": probe(1080)},
        probes_existing={existing.key: probe(720)},
    )
    assert decision.groups[0].verdict is GroupVerdict.MANUAL
    assert decision.groups[0].reason == "smaller_but_better"


def test_extra_dir_instances_count_as_existing() -> None:
    plan, snapshot = series_plan(incoming_episode(1, 300))
    decision = decide(plan, snapshot, [extra_file(1, 200)])
    group = decision.groups[0]
    assert group.verdict is GroupVerdict.REPLACE
    assert group.overlap[0].existing[0].extra_base == "/data/anirss"


def test_best_existing_instance_sets_the_bar() -> None:
    # Library already upgraded once; the extra dir still has the small TV rip.
    plan, snapshot = series_plan(incoming_episode(1, 210))
    decision = decide(plan, snapshot, [lib_file(1, 200), extra_file(1, 50)])
    group = decision.groups[0]
    assert group.overlap[0].existing_bytes == 200
    assert group.verdict is GroupVerdict.MANUAL  # 1.05, gray zone


def test_seasons_are_judged_independently() -> None:
    s1 = incoming_episode(1, 100)
    s2 = incoming_episode(1, 100)
    s2_move = Move(
        kind=MoveKind.MEDIA,
        source_root=Root.INBOUND,
        source_path="Drop/[BD] Show S2 - 01.mkv",
        dest_root=Root.LIBRARY,
        dest_path=f"{FOLDER}/S02/Show S02E01.mkv",
        candidate_id="V21",
    )
    s2_snapshot = SnapshotFile("V21", "[BD] Show S2 - 01.mkv", FileKind.VIDEO, 100)
    plan = Plan(identity=SERIES, moves=(s1[1], s2_move))
    decision = decide(plan, (s1[0], s2_snapshot), [lib_file(1, 100)])
    verdicts = {group.season: group.verdict for group in decision.groups}
    assert verdicts == {1: GroupVerdict.DISCARD, 2: GroupVerdict.IMPORT}


def test_movie_videos_pair_without_spans() -> None:
    move = Move(
        kind=MoveKind.MEDIA,
        source_root=Root.INBOUND,
        source_path="Drop/feature.bd.mkv",
        dest_root=Root.LIBRARY,
        dest_path="Feature (2016) {tmdb-456}/Feature (2016).mkv",
        candidate_id="V1",
    )
    snapshot = (SnapshotFile("V1", "feature.bd.mkv", FileKind.VIDEO, 300),)
    plan = Plan(identity=MOVIE, moves=(move,))
    existing = ExistingFile(
        root=Root.LIBRARY,
        extra_base=None,
        relative_path="Feature (2016) {tmdb-456}/Feature (2016).mp4",
        size_bytes=100,
        span=None,
    )
    decision = decide(plan, snapshot, [existing])
    assert decision.groups[0].season is None
    assert decision.groups[0].verdict is GroupVerdict.REPLACE


# ---- resolve -------------------------------------------------------------


def manual_decision() -> tuple[Plan, ReplacementDecision]:
    plan, snapshot = series_plan(incoming_episode(1, 110))
    return plan, decide(plan, snapshot, [lib_file(1, 100)])


def test_resolve_replace_flips_manual_groups() -> None:
    _, decision = manual_decision()
    resolved = resolve(decision, ReplaceResolution.REPLACE)
    assert resolved.groups[0].verdict is GroupVerdict.REPLACE
    assert not resolved.needs_confirmation


def test_resolve_discard_flips_manual_groups() -> None:
    _, decision = manual_decision()
    resolved = resolve(decision, ReplaceResolution.DISCARD_INCOMING)
    assert resolved.groups[0].verdict is GroupVerdict.DISCARD


def test_resolve_keep_both_leaves_the_plan_alone() -> None:
    plan, decision = manual_decision()
    resolved = resolve(decision, ReplaceResolution.KEEP_BOTH)
    assert apply_decision(plan, resolved, run_id=RUN_ID) == plan


def test_apply_refuses_unresolved_manual_groups() -> None:
    plan, decision = manual_decision()
    with pytest.raises(ReplaceError):
        apply_decision(plan, decision, run_id=RUN_ID)


# ---- apply_decision ------------------------------------------------------


def test_replacement_trashes_every_existing_instance_first() -> None:
    plan, snapshot = series_plan(incoming_episode(1, 300))
    library = lib_file(1, 200)
    extra = extra_file(1, 50)
    decision = decide(plan, snapshot, [library, extra])
    augmented = apply_decision(plan, decision, run_id=RUN_ID)

    kinds = [move.kind for move in augmented.moves]
    assert kinds == [
        MoveKind.TRASH_REPLACED,
        MoveKind.TRASH_REPLACED,
        MoveKind.MEDIA,
    ]
    trash_lib, trash_extra = augmented.moves[0], augmented.moves[1]
    assert trash_lib.source_root is Root.LIBRARY
    assert trash_lib.dest_path == (
        f"{TRASH_DIR}/{RUN_ID}/{library.relative_path}"
    )
    assert trash_extra.source_root is Root.EXTRA
    assert trash_extra.extra_base == "/data/anirss"
    assert trash_extra.dest_path == (
        f"{TRASH_DIR}/{RUN_ID}/{extra.relative_path}"
    )
    # The import move itself is untouched.
    assert augmented.moves[2] == plan.moves[0]


def test_s1_duplicates_are_trashed_while_s2_still_imports() -> None:
    s1e1 = incoming_episode(1, 100)
    s2_move = Move(
        kind=MoveKind.MEDIA,
        source_root=Root.INBOUND,
        source_path="Drop/[BD] Show S2 - 01.mkv",
        dest_root=Root.LIBRARY,
        dest_path=f"{FOLDER}/S02/Show S02E01.mkv",
        candidate_id="V21",
    )
    s2_snapshot = SnapshotFile("V21", "[BD] Show S2 - 01.mkv", FileKind.VIDEO, 150)
    plan = Plan(identity=SERIES, moves=(s1e1[1], s2_move))
    decision = decide(plan, (s1e1[0], s2_snapshot), [lib_file(1, 100)])
    augmented = apply_decision(plan, decision, run_id=RUN_ID)

    discarded = augmented.moves[0]
    assert discarded.kind is MoveKind.TRASH_DUPLICATE
    assert discarded.dest_root is Root.INBOUND
    assert discarded.dest_path == (
        f"{TRASH_DIR}/{RUN_ID}/Drop/[BD] Show - 01.mkv"
    )
    assert discarded.candidate_id == "V1"
    # S2 imports exactly as planned.
    assert augmented.moves[1] == s2_move


def test_folder_rename_redirects_library_trash_paths() -> None:
    pair = incoming_episode(1, 300)
    rename = Move(
        kind=MoveKind.FOLDER_RENAME,
        source_root=Root.LIBRARY,
        source_path="Show",
        dest_root=Root.LIBRARY,
        dest_path=FOLDER,
    )
    plan, snapshot = series_plan(pair, moves_prefix=(rename,))
    existing = ExistingFile(
        root=Root.LIBRARY,
        extra_base=None,
        relative_path="Show/S01/Show S01E01.mkv",
        size_bytes=100,
        span=EpisodeSpan(1, 1, 1),
    )
    augmented = apply_decision(
        plan, decide(plan, snapshot, [existing]), run_id=RUN_ID
    )
    trash = augmented.moves[1]
    assert trash.kind is MoveKind.TRASH_REPLACED
    # The rename has already happened by the time the trash move runs.
    assert trash.source_path == f"{FOLDER}/S01/Show S01E01.mkv"
    assert trash.dest_path == f"{TRASH_DIR}/{RUN_ID}/{FOLDER}/S01/Show S01E01.mkv"


def test_bundled_subtitle_follows_its_discarded_episode_only_if_present() -> None:
    video = incoming_episode(1, 100)
    subtitle_snapshot = SnapshotFile(
        "S1", "[BD] Show - 01.chs.ass", FileKind.SUBTITLE, 10, SubtitleVariant.CHS
    )
    subtitle_move = Move(
        kind=MoveKind.MEDIA,
        source_root=Root.INBOUND,
        source_path="Drop/[BD] Show - 01.chs.ass",
        dest_root=Root.LIBRARY,
        dest_path=f"{FOLDER}/S01/Show S01E01.chs.ass",
        candidate_id="S1",
    )
    plan = Plan(identity=SERIES, moves=(video[1], subtitle_move))
    snapshot = (video[0], subtitle_snapshot)

    # The library already holds that exact subtitle: discard the copy.
    existing_subtitle = ExistingFile(
        root=Root.LIBRARY,
        extra_base=None,
        relative_path=f"{FOLDER}/S01/Show S01E01.chs.ass",
        size_bytes=10,
        span=EpisodeSpan(1, 1, 1),
    )
    decision = decide(plan, snapshot, [lib_file(1, 100), existing_subtitle])
    augmented = apply_decision(plan, decision, run_id=RUN_ID)
    assert augmented.moves[1].kind is MoveKind.TRASH_DUPLICATE

    # The library lacks the subtitle: it still imports.
    decision = decide(plan, snapshot, [lib_file(1, 100)])
    augmented = apply_decision(plan, decision, run_id=RUN_ID)
    assert augmented.moves[1] == subtitle_move


def test_decision_json_round_trip() -> None:
    plan, snapshot = series_plan(incoming_episode(1, 110))
    decision = decide(plan, snapshot, [lib_file(1, 100)])
    encoded = decision.to_json()
    assert ReplacementDecision.from_json(encoded) == decision
