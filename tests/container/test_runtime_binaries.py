from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from reeloom.adapters.filesystem import (
    FFPROBE_EXECUTABLE,
    FFPROBE_LIMIT_EXECUTABLE,
    FilesystemScanner,
    FilesystemVideoSubtitleInspector,
)
from reeloom.adapters.subtitle_archive import (
    SEVENZIP_EXECUTABLE,
    SEVENZIP_LIMIT_EXECUTABLE,
    FilesystemSubtitleArchiveInspector,
)
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.subtitle_acquisition import (
    EmbeddedChineseStatus,
    EmbeddedSubtitleCodec,
    EmbeddedSubtitleInspection,
    EmbeddedSubtitleLanguage,
    EmbeddedSubtitleProbeStatus,
    SubtitleArchiveFormat,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleArchiveVolume,
    SubtitleReleaseId,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.subtitle_acquisition import (
    DownloadedArchiveVolume,
    DownloadedSubtitleArchiveSet,
)

CONTAINER_CONTRACT_ENV = "REELOOM_CONTAINER_CONTRACTS"
FFMPEG_EXECUTABLE = "/usr/bin/ffmpeg"
EXPECTED_FFPROBE_VERSION = "7.1.5-0+deb13u1"
EXPECTED_SEVENZIP_VERSION = "26.02"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _make_video(directory: Path, *, with_chinese_subtitle: bool) -> Path:
    directory.mkdir()
    output = directory / "Episode 01.mkv"
    arguments = [
        FFMPEG_EXECUTABLE,
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=16x16:r=1:d=1",
    ]
    if with_chinese_subtitle:
        subtitle = directory / "fixture.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:00,900\ncontract fixture\n",
            encoding="utf-8",
        )
        arguments.extend(
            (
                "-f",
                "srt",
                "-i",
                os.fspath(subtitle),
                "-map",
                "0:v:0",
                "-map",
                "1:s:0",
                "-c:v",
                "ffv1",
                "-c:s",
                "srt",
                "-metadata:s:s:0",
                "language=chi",
                "-disposition:s:0",
                "default",
                "-shortest",
            )
        )
    else:
        arguments.extend(("-map", "0:v:0", "-c:v", "ffv1"))
    arguments.append(os.fspath(output))
    _run(*arguments)
    return output


def _inspect_video(video: Path) -> EmbeddedSubtitleInspection:
    scan = FilesystemScanner().scan(AuthorizedRoot.create(video.parent))
    return asyncio.run(
        FilesystemVideoSubtitleInspector(scan).inspect(
            CandidateId(CandidateKind.VIDEO, 1),
            season_number=1,
        )
    )


def _downloaded_zip(archive: Path) -> DownloadedSubtitleArchiveSet:
    content = archive.read_bytes()
    metadata = archive.stat()
    volume = SubtitleArchiveVolume(
        index=1,
        attachment_id=101,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    capability = SubtitleArchiveSetCapability(
        archive_set_id=SubtitleArchiveSetId(1),
        release_id=SubtitleReleaseId(1),
        format=SubtitleArchiveFormat.ZIP,
        thread_id=10081,
        post_id=95257,
        attachment_ids=(101,),
        declared_size=len(content),
    )
    return DownloadedSubtitleArchiveSet(
        capability=capability,
        volumes=(
            DownloadedArchiveVolume(
                volume=volume,
                path=archive,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                mtime_ns=metadata.st_mtime_ns,
                ctime_ns=metadata.st_ctime_ns,
            ),
        ),
    )


@unittest.skipUnless(
    os.environ.get(CONTAINER_CONTRACT_ENV) == "1",
    "requires the built Reeloom container image",
)
class RuntimeBinaryContractTests(unittest.TestCase):
    def test_deployment_contains_the_expected_fixed_binaries(self) -> None:
        for executable in (
            FFMPEG_EXECUTABLE,
            FFPROBE_EXECUTABLE,
            FFPROBE_LIMIT_EXECUTABLE,
            SEVENZIP_EXECUTABLE,
            SEVENZIP_LIMIT_EXECUTABLE,
        ):
            self.assertTrue(os.access(executable, os.X_OK), executable)

        ffprobe_version = _run(FFPROBE_EXECUTABLE, "-version").stdout
        sevenzip_version = _run(SEVENZIP_EXECUTABLE, "i").stdout
        self.assertIn(
            f"ffprobe version {EXPECTED_FFPROBE_VERSION}",
            ffprobe_version.splitlines()[0],
        )
        self.assertIn(
            f"7-Zip (z) {EXPECTED_SEVENZIP_VERSION}",
            sevenzip_version,
        )

    def test_real_ffprobe_contract_distinguishes_embedded_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            without_subtitle = _inspect_video(
                _make_video(
                    root / "without-subtitle",
                    with_chinese_subtitle=False,
                )
            )
            with_subtitle = _inspect_video(
                _make_video(
                    root / "with-subtitle",
                    with_chinese_subtitle=True,
                )
            )

        self.assertIs(
            without_subtitle.probe_status,
            EmbeddedSubtitleProbeStatus.ABSENT,
        )
        self.assertIs(
            without_subtitle.chinese_status,
            EmbeddedChineseStatus.ABSENT,
        )
        self.assertEqual(without_subtitle.tracks, ())
        self.assertIs(
            with_subtitle.probe_status,
            EmbeddedSubtitleProbeStatus.PRESENT,
        )
        self.assertIs(
            with_subtitle.chinese_status,
            EmbeddedChineseStatus.PRESENT,
        )
        self.assertEqual(len(with_subtitle.tracks), 1)
        self.assertIs(
            with_subtitle.tracks[0].codec,
            EmbeddedSubtitleCodec.SUBRIP,
        )
        self.assertIs(
            with_subtitle.tracks[0].language,
            EmbeddedSubtitleLanguage.ZH,
        )

    def test_real_7zz_contract_lists_and_extracts_subtitle(self) -> None:
        subtitle_content = b"[Script Info]\nTitle: container contract\n"
        with tempfile.TemporaryDirectory() as raw_directory:
            archive = Path(raw_directory) / "subtitles.zip"
            with zipfile.ZipFile(
                archive,
                mode="x",
                compression=zipfile.ZIP_STORED,
            ) as file:
                file.writestr("Subs/E01.ass", subtitle_content)
            result = asyncio.run(
                FilesystemSubtitleArchiveInspector().inspect(
                    _downloaded_zip(archive),
                    season_numbers=(1,),
                )
            )

        self.assertEqual(len(result.members), 1)
        self.assertEqual(result.members[0].source_path.as_posix(), "Subs/E01.ass")
        self.assertEqual(result.members[0].size_bytes, len(subtitle_content))
        self.assertEqual(
            result.members[0].sha256,
            hashlib.sha256(subtitle_content).hexdigest(),
        )
        self.assertEqual(result.rejected_entries, ())


if __name__ == "__main__":
    unittest.main()
