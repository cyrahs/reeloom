from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reeloom.adapters.approval import FilesystemApprovalStore
from reeloom.adapters.filesystem import (
    FilesystemPlanCompiler,
    FilesystemScanner,
)
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.executor.errors import (
    ExecutorError,
    ExecutorErrorCode,
)
from reeloom.executor.preflight import FilesystemPreflightExecutor
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.naming import SeriesIdentity, SubtitleVariant
from reeloom.kernel.rename_plan import RenamePlan
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot

_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Environment:
    source: Path
    output: Path
    plan_store_root: AuthorizedRoot
    approval_store_root: AuthorizedRoot
    plan: RenamePlan
    approval: ApprovalRecord

    def executor(self) -> FilesystemPreflightExecutor:
        return FilesystemPreflightExecutor(
            plans=FilesystemPlanStore(self.plan_store_root),
        )


def _setup(tmp_path: Path, *, persist_plan: bool = True) -> _Environment:
    source = tmp_path / "incoming"
    output = tmp_path / "anime"
    plan_store_path = tmp_path / "plans"
    approval_store_path = tmp_path / "approvals"
    for path in (
        source,
        output,
        plan_store_path,
        approval_store_path,
    ):
        path.mkdir()
    (source / "episode.mkv").write_bytes(b"video")
    (source / "episode.ass").write_bytes("简体字幕".encode())
    (source / "unmapped.mkv").write_bytes(b"unmapped")

    scan = FilesystemScanner().scan(AuthorizedRoot.create(source))
    mapping = MappingDraft.from_dict(
        {
            "videos": [
                {
                    "video_id": "video:1",
                    "season": 1,
                    "episode_start": 2,
                    "episode_end": 2,
                }
            ],
            "subtitles": [
                {
                    "subtitle_id": "subtitle:1",
                    "video_id": "video:1",
                }
            ],
        },
        candidates=scan.snapshot.candidates,
        catalog=EpisodeCatalog.from_counts({1: 12}),
    )
    subtitle_id = next(
        record.candidate.id
        for record in scan.snapshot.records
        if record.candidate.kind is CandidateKind.SUBTITLE
    )
    plan = FilesystemPlanCompiler(
        scan=scan,
        output_root=AuthorizedRoot.create(output),
    ).compile(
        run_id="run-m6",
        work_type=TmdbWorkType.ANIME,
        series=SeriesIdentity(
            title_zh_cn="正确动画",
            year=2024,
            tmdb_id=200,
        ),
        mapping=mapping,
        subtitle_variants=((subtitle_id, SubtitleVariant.CHS),),
        created_at=_NOW,
    )
    plan_store_root = AuthorizedRoot.create(plan_store_path)
    if persist_plan:
        FilesystemPlanStore(plan_store_root).save(plan)
    approval = ApprovalRecord.create(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        scope=ApprovalScope.APPLY,
        expires_at=_NOW + timedelta(minutes=5),
        nonce="n" * 32,
    )
    approval_store_root = AuthorizedRoot.create(approval_store_path)
    FilesystemApprovalStore(
        approval_store_root,
        clock=lambda: _NOW,
    ).issue(approval)
    return _Environment(
        source=source,
        output=output,
        plan_store_root=plan_store_root,
        approval_store_root=approval_store_root,
        plan=plan,
        approval=approval,
    )


def _preflight(environment: _Environment):
    return environment.executor().preflight(
        plan_hash=environment.plan.plan_hash,
    )


def _plan_path(root: AuthorizedRoot, plan_hash: str) -> Path:
    return root.path / f"plan-v1-{plan_hash.removeprefix('sha256:')}.json"


def test_preflight_checks_exact_plan_without_changing_state(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    result = _preflight(environment)

    after_media = {
        path.relative_to(tmp_path): path.read_bytes()
        for root in (environment.source, environment.output)
        for path in root.rglob("*")
        if path.is_file()
    }
    before_media = {
        path: content
        for path, content in before.items()
        if path.parts[0] in {"incoming", "anime"}
    }
    assert result.plan_hash == environment.plan.plan_hash
    assert result.source_count == 3
    assert result.move_count == 2
    assert after_media == before_media
    assert tuple(environment.output.iterdir()) == ()
    assert len(tuple(environment.approval_store_root.path.iterdir())) == 1


def test_preflight_rejects_plan_tamper_before_claim(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path, persist_plan=False)
    tampered = environment.plan.canonical_bytes().replace(
        b'"episode_start":2',
        b'"episode_start":3',
    )
    _plan_path(
        environment.plan_store_root,
        environment.plan.plan_hash,
    ).write_bytes(tampered)

    with pytest.raises(ExecutorError) as raised:
        _preflight(environment)

    assert raised.value.code is ExecutorErrorCode.INVALID_PLAN
    assert len(tuple(environment.approval_store_root.path.iterdir())) == 1


@pytest.mark.parametrize(
    "relative_path",
    ("episode.mkv", "unmapped.mkv"),
)
def test_preflight_rejects_mapped_or_unmapped_source_drift(
    tmp_path: Path,
    relative_path: str,
) -> None:
    environment = _setup(tmp_path)
    source = environment.source / relative_path
    source.rename(environment.source / f"original-{relative_path}")
    source.write_bytes(b"replacement")

    with pytest.raises(ExecutorError) as raised:
        _preflight(environment)

    assert raised.value.code is ExecutorErrorCode.SOURCE_DRIFT
    assert tuple(environment.output.iterdir()) == ()


def test_preflight_rejects_source_symlink(tmp_path: Path) -> None:
    environment = _setup(tmp_path)
    source = environment.source / "episode.mkv"
    original = environment.source / "original-episode.mkv"
    source.rename(original)
    source.symlink_to(original)

    with pytest.raises(ExecutorError) as raised:
        _preflight(environment)

    assert raised.value.code is ExecutorErrorCode.SYMLINK_NOT_ALLOWED
    assert tuple(environment.output.iterdir()) == ()


def test_preflight_fails_closed_when_source_changes_during_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path)
    source = environment.source / "episode.mkv"
    original = environment.source / "original-episode.mkv"
    real_open = os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "episode.mkv" and not swapped:
            swapped = True
            source.rename(original)
            source.symlink_to(original)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_open)

    with pytest.raises(ExecutorError) as raised:
        _preflight(environment)

    assert swapped
    assert raised.value.code is ExecutorErrorCode.PREFLIGHT_FAILED
    assert tuple(environment.output.iterdir()) == ()


def test_preflight_rejects_destination_that_appeared(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path)
    destination = environment.output / Path(
        environment.plan.draft.moves[0].destination
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")

    with pytest.raises(ExecutorError) as raised:
        _preflight(environment)

    assert raised.value.code is ExecutorErrorCode.DESTINATION_COLLISION
    assert destination.read_bytes() == b"existing"


def test_preflight_rejects_symlinked_destination_parent(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    series_root = environment.output / "正确动画 (2024) {tmdb-200}"
    series_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ExecutorError) as raised:
        _preflight(environment)

    assert raised.value.code is ExecutorErrorCode.SYMLINK_NOT_ALLOWED
    assert tuple(outside.iterdir()) == ()


def test_preflight_rejects_root_identity_drift(tmp_path: Path) -> None:
    environment = _setup(tmp_path)
    environment.source.rename(tmp_path / "original-incoming")
    environment.source.mkdir()

    with pytest.raises(ExecutorError) as raised:
        _preflight(environment)

    assert raised.value.code is ExecutorErrorCode.ROOT_DRIFT
    assert tuple(environment.output.iterdir()) == ()


def test_preflight_rejects_cross_filesystem_manifest(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path, persist_plan=False)
    payload = json.loads(environment.plan.canonical_bytes())
    payload["roots"]["output"]["device"] += 1
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    plan_hash = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    _plan_path(environment.plan_store_root, plan_hash).write_bytes(canonical)
    approval = ApprovalRecord.create(
        run_id=environment.plan.run_id,
        plan_hash=plan_hash,
        scope=ApprovalScope.APPLY,
        expires_at=_NOW + timedelta(minutes=5),
        nonce="x" * 32,
    )
    FilesystemApprovalStore(
        environment.approval_store_root,
        clock=lambda: _NOW,
    ).issue(approval)

    with pytest.raises(ExecutorError) as raised:
        environment.executor().preflight(
            plan_hash=plan_hash,
        )

    assert raised.value.code is ExecutorErrorCode.CROSS_FILESYSTEM


def test_successful_preflight_does_not_consume_approval(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path)
    _preflight(environment)
    _preflight(environment)

    assert len(tuple(environment.approval_store_root.path.iterdir())) == 1
