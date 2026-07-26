from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath

from psycopg_pool import ConnectionPool

from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.apply import ApplyResult, ApplyStatus
from reeloom.executor.manifest import ExecutionManifest
from reeloom.executor.preflight import FilesystemPreflightExecutor
from reeloom.runtime.event_codec import encode_event
from reeloom.runtime.events import ExecutionSettled
from reeloom.runtime.state_codec import (
    STATE_PROJECTION_SCHEMA,
    patch_state,
)
from reeloom.kernel.amendment import CompletedLayout, CompletedLayoutFile
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.candidates import Candidate
from reeloom.kernel.scanner import (
    CandidateRecord,
    ScannedCandidateSnapshot,
    rebuild_candidate_snapshot,
)
from reeloom.adapters.filesystem import FilesystemScanner
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.kernel.rename_plan import RootBinding
from reeloom.server.errors import ServerError, ServerErrorCode


def capture_completed_layout(
    manifest: ExecutionManifest,
    *,
    transaction_id: str,
) -> CompletedLayout:
    moved = {item.source_id: item.destination for item in manifest.moves}
    root_fd = FilesystemPreflightExecutor._open_bound_root(
        manifest.output_root
    )
    files: list[CompletedLayoutFile] = []
    try:
        included = (
            manifest.sources
            if manifest.source_root == manifest.output_root
            else tuple(
                source
                for source in manifest.sources
                if source.candidate_id in moved
            )
        )
        for source in included:
            path = moved.get(source.candidate_id, source.relative_path)
            current_fd = root_fd
            file_fd: int | None = None
            try:
                for part in path.parts[:-1]:
                    next_fd = (
                        FilesystemPreflightExecutor._open_existing_directory(
                            current_fd,
                            part,
                            missing_code=ExecutorErrorCode.SOURCE_DRIFT,
                            nondirectory_code=ExecutorErrorCode.SOURCE_DRIFT,
                        )
                    )
                    if current_fd != root_fd:
                        os.close(current_fd)
                    current_fd = next_fd
                before = os.stat(
                    path.name,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(before.st_mode):
                    raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
                file_fd = os.open(
                    path.name,
                    os.O_RDONLY
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=current_fd,
                )
                opened = os.fstat(file_fd)
                if (
                    before.st_dev != opened.st_dev
                    or before.st_ino != opened.st_ino
                    or before.st_size != opened.st_size
                    or before.st_mtime_ns != opened.st_mtime_ns
                    or before.st_ctime_ns != opened.st_ctime_ns
                ):
                    raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
                digest = None
                if source.kind is CandidateKind.SUBTITLE:
                    content = os.read(file_fd, 64 * 1024)
                    digest = hashlib.sha256(content).hexdigest()
                after = os.fstat(file_fd)
                if (
                    opened.st_dev != after.st_dev
                    or opened.st_ino != after.st_ino
                    or opened.st_size != after.st_size
                    or opened.st_mtime_ns != after.st_mtime_ns
                    or opened.st_ctime_ns != after.st_ctime_ns
                ):
                    raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
                files.append(
                    CompletedLayoutFile(
                        candidate_id=source.candidate_id,
                        kind=source.kind,
                        relative_path=path,
                        size_bytes=after.st_size,
                        device=after.st_dev,
                        inode=after.st_ino,
                        mtime_ns=after.st_mtime_ns,
                        ctime_ns=after.st_ctime_ns,
                        sample_digest=digest,
                    )
                )
            finally:
                if file_fd is not None:
                    os.close(file_fd)
                if current_fd != root_fd:
                    os.close(current_fd)
    finally:
        os.close(root_fd)
    return CompletedLayout(
        run_id=manifest.run_id,
        original_plan_hash=manifest.plan_hash,
        transaction_id=transaction_id,
        root=manifest.output_root,
        files=tuple(files),
    )


def _payload(layout: CompletedLayout) -> str:
    return json.dumps(
        {
            "files": [
                {
                    "candidate_id": str(item.candidate_id),
                    "ctime_ns": item.ctime_ns,
                    "device": item.device,
                    "inode": item.inode,
                    "kind": item.kind.value,
                    "mtime_ns": item.mtime_ns,
                    "relative_path": item.relative_path.as_posix(),
                    "sample_digest": item.sample_digest,
                    "size_bytes": item.size_bytes,
                }
                for item in layout.files
            ],
            "original_plan_hash": layout.original_plan_hash,
            "root": {
                "device": layout.root.device,
                "inode": layout.root.inode,
                "path": layout.root.path.as_posix(),
            },
            "run_id": layout.run_id,
            "transaction_id": layout.transaction_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode(value: object) -> CompletedLayout:
    raw = value if isinstance(value, dict) else json.loads(str(value))
    root = raw["root"]
    return CompletedLayout(
        run_id=raw["run_id"],
        original_plan_hash=raw["original_plan_hash"],
        transaction_id=raw["transaction_id"],
        root=RootBinding(
            PurePosixPath(root["path"]),
            root["device"],
            root["inode"],
        ),
        files=tuple(
            CompletedLayoutFile(
                candidate_id=CandidateId.parse(item["candidate_id"]),
                kind=CandidateKind(item["kind"]),
                relative_path=PurePosixPath(item["relative_path"]),
                size_bytes=item["size_bytes"],
                device=item["device"],
                inode=item["inode"],
                mtime_ns=item["mtime_ns"],
                ctime_ns=item["ctime_ns"],
                sample_digest=item["sample_digest"],
            )
            for item in raw["files"]
        ),
    )


class PostgresCompletedLayoutRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def settle_and_append(
        self,
        *,
        result: ApplyResult,
        layout: CompletedLayout | None,
    ) -> int | None:
        if (result.status is ApplyStatus.COMPLETED) != (layout is not None):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    existing = connection.execute(
                        """
                        SELECT status, applied_count, rolled_back_count,
                               failure_code, transaction_id
                        FROM approval_settlements
                        WHERE approval_id = %s
                        """,
                        (result.approval_id,),
                    ).fetchone()
                    if existing is not None:
                        expected_failure = (
                            None
                            if result.failure_code is None
                            else result.failure_code.value
                        )
                        if (
                            str(existing[0]) != result.status.value
                            or int(existing[1]) != result.applied_count
                            or int(existing[2]) != result.rolled_back_count
                            or existing[3] != expected_failure
                            or str(existing[4]) != result.transaction_id
                        ):
                            raise ServerError(
                                ServerErrorCode.INTERACTION_CONFLICT
                            )
                        head = connection.execute(
                            """
                            SELECT version FROM completed_layouts
                            WHERE transaction_id = %s
                            """,
                            (result.transaction_id,),
                        ).fetchone()
                        return None if head is None else int(head[0])
                    connection.execute(
                        """
                        INSERT INTO approval_settlements
                            (approval_id, transaction_id, status,
                             applied_count, rolled_back_count, failure_code)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (approval_id) DO NOTHING
                        """,
                        (
                            result.approval_id,
                            result.transaction_id,
                            result.status.value,
                            result.applied_count,
                            result.rolled_back_count,
                            (
                                None
                                if result.failure_code is None
                                else result.failure_code.value
                            ),
                        ),
                    )
                    approval = connection.execute(
                        """
                        SELECT run_id, plan_hash
                        FROM approvals
                        WHERE approval_id = %s
                        """,
                        (result.approval_id,),
                    ).fetchone()
                    if (
                        approval is None
                        or str(approval[1]) != result.plan_hash
                        or (
                            layout is not None
                            and layout.run_id != str(approval[0])
                        )
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    run_id = str(approval[0])
                    runtime = connection.execute(
                        """
                        SELECT event_sequence, phase, plan_hash,
                               projection_schema, projection_payload
                        FROM run_states
                        WHERE run_id = %s
                        FOR UPDATE
                        """,
                        (run_id,),
                    ).fetchone()
                    if (
                        runtime is None
                        or str(runtime[1]) != "awaiting_approval"
                        or str(runtime[2]) != result.plan_hash
                        or str(runtime[3]) != STATE_PROJECTION_SCHEMA
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    event = ExecutionSettled(
                        plan_hash=result.plan_hash,
                        approval_id=result.approval_id,
                        transaction_id=result.transaction_id,
                        status=result.status.value,
                        applied_count=result.applied_count,
                        rolled_back_count=result.rolled_back_count,
                        failure_code=(
                            None
                            if result.failure_code is None
                            else result.failure_code.value
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO run_events
                            (run_id, sequence, event_type, payload)
                        VALUES (%s, %s, 'execution_settled', %s)
                        """,
                        (
                            run_id,
                            int(runtime[0]) + 1,
                            encode_event(event),
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE run_states
                        SET event_sequence = event_sequence + 1,
                            phase = %s, runtime_status = 'stopped',
                            projection_payload = %s::jsonb,
                            updated_at = clock_timestamp()
                        WHERE run_id = %s
                        """,
                        (
                            (
                                "completed"
                                if result.status is ApplyStatus.COMPLETED
                                else "rolled_back"
                            ),
                            patch_state(
                                runtime[4],
                                applied_count=result.applied_count,
                                approval_id=result.approval_id,
                                event_count=int(runtime[0]) + 1,
                                failure_code=(
                                    None
                                    if result.failure_code is None
                                    else result.failure_code.value
                                ),
                                phase=(
                                    "completed"
                                    if result.status
                                    is ApplyStatus.COMPLETED
                                    else "rolled_back"
                                ),
                                rolled_back_count=(
                                    result.rolled_back_count
                                ),
                                status="stopped",
                                stop_reason=None,
                                transaction_id=result.transaction_id,
                            ),
                            run_id,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE runs SET status = %s WHERE run_id = %s
                        """,
                        (result.status.value, run_id),
                    )
                    if layout is None:
                        return None
                    row = connection.execute(
                        """
                        SELECT version FROM completed_layout_heads
                        WHERE run_id = %s
                        FOR UPDATE
                        """,
                        (layout.run_id,),
                    ).fetchone()
                    version = 1 if row is None else int(row[0]) + 1
                    connection.execute(
                        """
                        INSERT INTO completed_layouts
                            (run_id, version, plan_hash, transaction_id,
                             layout_payload)
                        VALUES (%s, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            layout.run_id,
                            version,
                            layout.original_plan_hash,
                            layout.transaction_id,
                            _payload(layout),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO completed_layout_heads
                            (run_id, version, plan_hash)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (run_id) DO UPDATE SET
                            version = EXCLUDED.version,
                            plan_hash = EXCLUDED.plan_hash
                        """,
                        (
                            layout.run_id,
                            version,
                            layout.original_plan_hash,
                        ),
                    )
                    return version
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def settlement(
        self,
        *,
        run_id: str,
        plan_hash: str,
        approval_id: str,
    ) -> ApplyResult | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT s.transaction_id, s.status, s.applied_count,
                           s.rolled_back_count, s.failure_code
                    FROM approval_settlements AS s
                    JOIN approvals AS a USING (approval_id)
                    WHERE s.approval_id = %s
                      AND a.run_id = %s
                      AND a.plan_hash = %s
                    """,
                    (approval_id, run_id, plan_hash),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        if row is None:
            return None
        return ApplyResult(
            transaction_id=str(row[0]),
            plan_hash=plan_hash,
            approval_id=approval_id,
            status=ApplyStatus(str(row[1])),
            applied_count=int(row[2]),
            rolled_back_count=int(row[3]),
            failure_code=(
                None
                if row[4] is None
                else ExecutorErrorCode(str(row[4]))
            ),
        )

    def settlement_for_plan(
        self,
        *,
        run_id: str,
        plan_hash: str,
    ) -> ApplyResult | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT s.approval_id, s.transaction_id, s.status,
                           s.applied_count, s.rolled_back_count,
                           s.failure_code
                    FROM approval_settlements AS s
                    JOIN approvals AS a USING (approval_id)
                    WHERE a.run_id = %s AND a.plan_hash = %s
                    """,
                    (run_id, plan_hash),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        if row is None:
            return None
        return ApplyResult(
            transaction_id=str(row[1]),
            plan_hash=plan_hash,
            approval_id=str(row[0]),
            status=ApplyStatus(str(row[2])),
            applied_count=int(row[3]),
            rolled_back_count=int(row[4]),
            failure_code=(
                None
                if row[5] is None
                else ExecutorErrorCode(str(row[5]))
            ),
        )

    def head(self, run_id: str) -> CompletedLayout | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT l.layout_payload
                    FROM completed_layout_heads AS h
                    JOIN completed_layouts AS l
                      ON l.run_id = h.run_id AND l.version = h.version
                    WHERE h.run_id = %s
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        return None if row is None else _decode(row[0])


def revalidate_completed_layout(
    layout: CompletedLayout,
) -> ScannedCandidateSnapshot:
    """Re-scan one bound archive root and require every completed identity."""

    root = AuthorizedRoot.create(Path(layout.root.path.as_posix()))
    if root.device != layout.root.device or root.inode != layout.root.inode:
        raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
    scanned = FilesystemScanner().scan(root).snapshot
    current = {item.relative_path: item for item in scanned.records}
    records: list[CandidateRecord] = []
    for expected in layout.files:
        observed = current.get(expected.relative_path)
        if (
            observed is None
            or observed.candidate.kind is not expected.kind
            or observed.size_bytes != expected.size_bytes
            or observed.device != expected.device
            or observed.inode != expected.inode
            or observed.mtime_ns != expected.mtime_ns
            or observed.ctime_ns != expected.ctime_ns
            or observed.sample_digest != expected.sample_digest
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        records.append(
            CandidateRecord(
                candidate=Candidate(
                    id=expected.candidate_id,
                    kind=expected.kind,
                    display_name=expected.relative_path.as_posix(),
                ),
                relative_path=expected.relative_path,
                size_bytes=expected.size_bytes,
                device=expected.device,
                inode=expected.inode,
                mtime_ns=expected.mtime_ns,
                ctime_ns=expected.ctime_ns,
                sample_digest=expected.sample_digest,
            )
        )
    return rebuild_candidate_snapshot(records)
