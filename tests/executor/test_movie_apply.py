from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import reeloom.executor.apply as apply_module
from reeloom.adapters.approval import FilesystemApprovalStore
from reeloom.adapters.filesystem import (
    FilesystemPlanCompiler,
    FilesystemScanner,
)
from reeloom.adapters.journal import FilesystemJournalStore
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.executor.apply import ApplyStatus, FilesystemExecutor
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.manifest import ExecutionManifest
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.movie import MovieMappingDraft
from reeloom.kernel.movie_amendment import compile_movie_amendment
from reeloom.kernel.naming import MovieIdentity, SubtitleVariant
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.completed_layout import capture_completed_layout


def _roots(tmp_path: Path):
    roots = tuple(
        tmp_path / name
        for name in ("watch", "archive", "plans", "approvals", "journals")
    )
    for root in roots:
        root.mkdir()
    return roots


def _plan(tmp_path: Path):
    watch, archive, plans_root, approvals_root, journals_root = _roots(
        tmp_path
    )
    (watch / "movie.mkv").write_bytes(b"movie")
    (watch / "movie.chs.srt").write_text("字幕", encoding="utf-8")
    scan = FilesystemScanner().scan(AuthorizedRoot.create(watch))
    mapping = MovieMappingDraft.from_dict(
        {"video_id": "video:1", "subtitle_ids": ["subtitle:1"]},
        candidates=scan.snapshot.candidates,
    )
    plan = FilesystemPlanCompiler(
        scan,
        AuthorizedRoot.create(archive),
    ).compile_movie(
        run_id="run-movie",
        movie=MovieIdentity("电影", 2024, 99),
        mapping=mapping,
        subtitle_variants=(
            (mapping.subtitle_ids[0], SubtitleVariant.CHS),
        ),
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    return (
        plan,
        watch,
        archive,
        plans_root,
        approvals_root,
        journals_root,
    )


def test_movie_plan_applies_through_existing_executor(tmp_path: Path) -> None:
    plan, watch, archive, plans_root, approvals_root, journals_root = _plan(
        tmp_path
    )
    plans = FilesystemPlanStore(AuthorizedRoot.create(plans_root))
    plans.save(plan)
    now = datetime(2026, 7, 26, tzinfo=UTC)
    approval = ApprovalRecord.create(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        scope=ApprovalScope.APPLY,
        expires_at=now + timedelta(minutes=5),
        nonce="m" * 32,
    )
    approvals = FilesystemApprovalStore(
        AuthorizedRoot.create(approvals_root),
        clock=lambda: now,
    )
    approvals.issue(approval)

    result = FilesystemExecutor(
        plans=plans,
        approvals=approvals,
        journals=FilesystemJournalStore(
            AuthorizedRoot.create(journals_root)
        ),
    ).apply(
        plan_hash=plan.plan_hash,
        approval_id=approval.approval_id,
    )

    movie_root = archive / "电影 (2024) {tmdb-99}"
    assert result.status is ApplyStatus.COMPLETED
    assert not (watch / "movie.mkv").exists()
    assert (movie_root / "电影 (2024).mkv").read_bytes() == b"movie"
    assert (movie_root / "电影 (2024).chs.srt").exists()


def test_movie_apply_rejects_created_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, watch, archive, plans_root, approvals_root, journals_root = _plan(
        tmp_path
    )
    plans = FilesystemPlanStore(AuthorizedRoot.create(plans_root))
    plans.save(plan)
    now = datetime(2026, 7, 26, tzinfo=UTC)
    approval = ApprovalRecord.create(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        scope=ApprovalScope.APPLY,
        expires_at=now + timedelta(minutes=5),
        nonce="t" * 32,
    )
    approvals = FilesystemApprovalStore(
        AuthorizedRoot.create(approvals_root),
        clock=lambda: now,
    )
    approvals.issue(approval)
    canonical = archive / "电影 (2024) {tmdb-99}"
    displaced = archive / "displaced"
    real_rename = apply_module._rename_noreplace
    calls = 0

    def replace_after_first_move(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal calls
        real_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )
        calls += 1
        if calls == 1:
            canonical.rename(displaced)
            canonical.mkdir()

    monkeypatch.setattr(
        apply_module,
        "_rename_noreplace",
        replace_after_first_move,
    )
    result = FilesystemExecutor(
        plans=plans,
        approvals=approvals,
        journals=FilesystemJournalStore(
            AuthorizedRoot.create(journals_root)
        ),
    ).apply(
        plan_hash=plan.plan_hash,
        approval_id=approval.approval_id,
    )

    assert result.status is ApplyStatus.ROLLED_BACK
    assert (watch / "movie.mkv").read_bytes() == b"movie"
    assert (watch / "movie.chs.srt").exists()
    assert not tuple(canonical.iterdir())
    assert not tuple(displaced.iterdir())


def test_movie_compiler_rejects_existing_canonical_directory(
    tmp_path: Path,
) -> None:
    watch, archive, *_ = _roots(tmp_path)
    (watch / "movie.mkv").write_bytes(b"movie")
    (archive / "电影 (2024) {tmdb-99}").mkdir()
    scan = FilesystemScanner().scan(AuthorizedRoot.create(watch))
    mapping = MovieMappingDraft.from_dict(
        {"video_id": "video:1", "subtitle_ids": []},
        candidates=scan.snapshot.candidates,
    )

    with pytest.raises(DomainError) as raised:
        FilesystemPlanCompiler(
            scan,
            AuthorizedRoot.create(archive),
        ).compile_movie(
            run_id="run-movie",
            movie=MovieIdentity("电影", 2024, 99),
            mapping=mapping,
            subtitle_variants=(),
            created_at=datetime(2026, 7, 26, tzinfo=UTC),
        )

    assert raised.value.code is ErrorCode.DESTINATION_COLLISION


def test_movie_compiler_rejects_nfkc_equivalent_directory(
    tmp_path: Path,
) -> None:
    watch, archive, *_ = _roots(tmp_path)
    (watch / "movie.mkv").write_bytes(b"movie")
    (archive / "电影 （2024） ｛tmdb-99｝").mkdir()
    scan = FilesystemScanner().scan(AuthorizedRoot.create(watch))
    mapping = MovieMappingDraft.from_dict(
        {"video_id": "video:1", "subtitle_ids": []},
        candidates=scan.snapshot.candidates,
    )

    with pytest.raises(DomainError) as raised:
        FilesystemPlanCompiler(
            scan,
            AuthorizedRoot.create(archive),
        ).compile_movie(
            run_id="run-movie",
            movie=MovieIdentity("电影", 2024, 99),
            mapping=mapping,
            subtitle_variants=(),
            created_at=datetime(2026, 7, 26, tzinfo=UTC),
        )

    assert raised.value.code is ErrorCode.DESTINATION_COLLISION


def test_movie_apply_rejects_nfkc_equivalent_directory(
    tmp_path: Path,
) -> None:
    plan, _, archive, plans_root, approvals_root, journals_root = _plan(
        tmp_path
    )
    (archive / "电影 （2024） ｛tmdb-99｝").mkdir()
    plans = FilesystemPlanStore(AuthorizedRoot.create(plans_root))
    plans.save(plan)
    now = datetime(2026, 7, 26, tzinfo=UTC)
    approval = ApprovalRecord.create(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        scope=ApprovalScope.APPLY,
        expires_at=now + timedelta(minutes=5),
        nonce="e" * 32,
    )
    approvals = FilesystemApprovalStore(
        AuthorizedRoot.create(approvals_root),
        clock=lambda: now,
    )
    approvals.issue(approval)

    with pytest.raises(ExecutorError) as raised:
        FilesystemExecutor(
            plans=plans,
            approvals=approvals,
            journals=FilesystemJournalStore(
                AuthorizedRoot.create(journals_root)
            ),
        ).apply(
            plan_hash=plan.plan_hash,
            approval_id=approval.approval_id,
        )

    assert raised.value.code is ExecutorErrorCode.DESTINATION_COLLISION


def test_movie_identity_reapply_uses_existing_executor(
    tmp_path: Path,
) -> None:
    plan, _, archive, plans_root, approvals_root, journals_root = _plan(
        tmp_path
    )
    plans = FilesystemPlanStore(AuthorizedRoot.create(plans_root))
    plans.save(plan)
    now = datetime(2026, 7, 26, tzinfo=UTC)
    approvals = FilesystemApprovalStore(
        AuthorizedRoot.create(approvals_root),
        clock=lambda: now,
    )
    first_approval = ApprovalRecord.create(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        scope=ApprovalScope.APPLY,
        expires_at=now + timedelta(minutes=5),
        nonce="i" * 32,
    )
    approvals.issue(first_approval)
    executor = FilesystemExecutor(
        plans=plans,
        approvals=approvals,
        journals=FilesystemJournalStore(
            AuthorizedRoot.create(journals_root)
        ),
    )
    first = executor.apply(
        plan_hash=plan.plan_hash,
        approval_id=first_approval.approval_id,
    )
    layout = capture_completed_layout(
        ExecutionManifest.from_canonical_bytes(
            plan.canonical_bytes(),
            plan_hash=plan.plan_hash,
        ),
        transaction_id=first.transaction_id,
    )
    amendment = compile_movie_amendment(
        layout=layout,
        movie=MovieIdentity("更正电影", 2025, 100),
        subtitle_variants=(
            (plan.draft.mapping.subtitle_ids[0], SubtitleVariant.CHS),
        ),
        created_at=now,
    )
    assert amendment is not None
    plans.save_movie_amendment(amendment)
    second_approval = ApprovalRecord.create(
        run_id=plan.run_id,
        plan_hash=amendment.plan_hash,
        scope=ApprovalScope.APPLY,
        expires_at=now + timedelta(minutes=5),
        nonce="r" * 32,
    )
    approvals.issue(second_approval)

    second = executor.apply(
        plan_hash=amendment.plan_hash,
        approval_id=second_approval.approval_id,
    )

    corrected = archive / "更正电影 (2025) {tmdb-100}"
    assert second.status is ApplyStatus.COMPLETED
    assert (corrected / "更正电影 (2025).mkv").exists()
    assert (corrected / "更正电影 (2025).chs.srt").exists()
