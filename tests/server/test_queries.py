from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import cast

import pytest
from psycopg_pool import ConnectionPool

from reeloom.kernel.amendment import (
    CompletedLayout,
    CompletedLayoutFile,
    DesiredLayoutMove,
    compile_amendment,
)
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.rename_plan import RootBinding
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.queries import PostgresQueries


class _Cursor:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...]:
        return self._row


class _Connection:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(
        self,
        query: object,
        parameters: object,
    ) -> _Cursor:
        del query, parameters
        return _Cursor(self._row)


class _Pool:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row

    def connection(self) -> _Connection:
        return _Connection(self._row)


class _Plans:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def load(self, plan_hash: str) -> bytes:
        del plan_hash
        return self._content


def _amendment() -> tuple[str, bytes]:
    candidate_id = CandidateId(CandidateKind.VIDEO, 1)
    layout = CompletedLayout(
        run_id="run-1",
        original_plan_hash="sha256:" + "a" * 64,
        transaction_id="txn-v1-" + "b" * 64,
        root=RootBinding(PurePosixPath("/archive"), 1, 2),
        files=(
            CompletedLayoutFile(
                candidate_id=candidate_id,
                kind=CandidateKind.VIDEO,
                relative_path=PurePosixPath(
                    "Series/S01/Series - S01E01.mkv"
                ),
                size_bytes=5,
                device=1,
                inode=10,
                mtime_ns=20,
                ctime_ns=30,
                sample_digest=None,
            ),
        ),
    )
    plan = compile_amendment(
        layout=layout,
        desired=(
            DesiredLayoutMove(
                source_id=candidate_id,
                video_id=candidate_id,
                destination=PurePosixPath(
                    "Series/S00/Series - S00E01.mkv"
                ),
                season=0,
                episode_start=1,
                episode_end=1,
            ),
        ),
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    assert plan is not None
    return plan.plan_hash, plan.canonical_bytes()


def test_preview_rejects_amendment_mislabeled_as_initial() -> None:
    plan_hash, content = _amendment()
    queries = PostgresQueries(
        cast(ConnectionPool, _Pool((plan_hash, "initial"))),
        plans=_Plans(content),
    )

    with pytest.raises(ServerError) as raised:
        queries.get_plan_preview(
            run_id="run-1",
            version=2,
            after=0,
            limit=50,
        )

    assert raised.value.code is ServerErrorCode.INTERACTION_CONFLICT
