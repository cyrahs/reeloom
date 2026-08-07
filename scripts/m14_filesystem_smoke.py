#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
import uuid
from pathlib import Path, PurePosixPath

from reeloom.adapters.forward_filesystem import PosixForwardFilesystem
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.forward_execution import PathObservationState
from reeloom.kernel.semantic_identity import (
    SemanticRootBinding,
    SemanticSourceIdentity,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an opt-in M14 filesystem conformance smoke."
    )
    parser.add_argument(
        "--live-filesystem",
        required=True,
        type=Path,
        metavar="EMPTY_THROWAWAY_DIRECTORY",
    )
    return parser


def run(root: Path) -> dict[str, object]:
    if not root.is_absolute():
        raise ValueError("throwaway directory must be absolute")
    metadata = os.lstat(root)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("throwaway path must be a real directory")
    if any(root.iterdir()):
        raise ValueError("throwaway directory must be empty")
    smoke = root / f"reeloom-m14-smoke-{uuid.uuid4().hex}"
    source = smoke / "source"
    destination = smoke / "library"
    os.mkdir(smoke, 0o700)
    os.mkdir(source, 0o700)
    os.mkdir(destination, 0o700)
    content = b"reeloom-m14-forward-smoke\n"
    descriptor = os.open(
        source / "sample.mkv",
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    identity = SemanticSourceIdentity(
        CandidateId(CandidateKind.VIDEO, 1),
        CandidateKind.VIDEO,
        PurePosixPath("sample.mkv"),
        len(content),
    )
    filesystem = PosixForwardFilesystem()
    source_root = SemanticRootBinding(PurePosixPath(source.as_posix()))
    output_root = SemanticRootBinding(PurePosixPath(destination.as_posix()))
    before = filesystem.observe(
        root=source_root,
        relative_path=identity.relative_path,
        expected=identity,
    )
    effect = filesystem.move(
        source_root=source_root,
        source_path=identity.relative_path,
        expected=identity,
        destination_root=output_root,
        destination_path=PurePosixPath("Movie/sample.mkv"),
    )
    observed = (PathObservationState.UNAVAILABLE,) * 2
    for delay in (0.0, 0.05, 0.2, 0.5, 1.0):
        if delay:
            time.sleep(delay)
        observed = (
            filesystem.observe(
                root=source_root,
                relative_path=identity.relative_path,
                expected=identity,
            ),
            filesystem.observe(
                root=output_root,
                relative_path=PurePosixPath("Movie/sample.mkv"),
                expected=identity,
            ),
        )
        if observed == (
            PathObservationState.ABSENT,
            PathObservationState.MATCHING,
        ):
            break
    if before is not PathObservationState.MATCHING or observed != (
        PathObservationState.ABSENT,
        PathObservationState.MATCHING,
    ):
        raise RuntimeError("filesystem did not converge after rename")
    return {
        "backend": effect.diagnostic.value,
        "result": "completed",
        "smoke_directory": smoke.as_posix(),
        "warnings": list(effect.warnings),
    }


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = run(arguments.live_filesystem)
    except Exception as error:
        print(
            json.dumps(
                {"error": type(error).__name__, "result": "failed"},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
