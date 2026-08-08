from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from reeloom.kernel.naming import filesystem_name_key
from reeloom.kernel.subtitle_publication import (
    SUBTITLE_PUBLICATION_MARKER,
    SubtitlePublicationManifest,
    SubtitlePublicationMember,
)
from reeloom.policy.path_policy import AuthorizedRoot, is_forbidden_env_name


class SubtitlePublicationState(StrEnum):
    COMPLETED = "completed"
    COLLISION = "collision"
    UNSAFE = "unsafe"
    UNAVAILABLE = "unavailable"


class SubtitlePublicationContentSource(Protocol):
    async def read_member(
        self,
        member: SubtitlePublicationMember,
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class SubtitlePublicationResult:
    state: SubtitlePublicationState
    publication_directory: str
    published_count: int
    warnings: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SubtitleMarkerPublisher:
    """Forward-only publication into a plan-owned final directory."""

    async def publish(
        self,
        *,
        root: AuthorizedRoot,
        source_folder: str,
        manifest: SubtitlePublicationManifest,
        content_source: SubtitlePublicationContentSource,
    ) -> SubtitlePublicationResult:
        if (
            not isinstance(root, AuthorizedRoot)
            or not isinstance(source_folder, str)
            or not source_folder
            or source_folder in {".", ".."}
            or "/" in source_folder
            or "\\" in source_folder
            or is_forbidden_env_name(source_folder)
            or not isinstance(manifest, SubtitlePublicationManifest)
        ):
            return self._result(
                manifest,
                SubtitlePublicationState.UNSAFE,
                reason="invalid_binding",
            )
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            return self._result(
                manifest,
                SubtitlePublicationState.UNAVAILABLE,
                reason="no_follow_unavailable",
            )
        root_fd: int | None = None
        source_fd: int | None = None
        publication_fd: int | None = None
        warnings: list[str] = []
        try:
            root_fd = os.open(
                root.path,
                os.O_RDONLY
                | os.O_DIRECTORY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0),
            )
            source_fd = self._open_directory(root_fd, source_folder)
            publication_fd = self._open_or_create_publication(
                source_fd,
                manifest.publication_directory,
            )
            unsafe = self._unexpected_entry_state(publication_fd, manifest)
            if unsafe is not None:
                state, reason = unsafe
                return self._result(manifest, state, reason=reason)

            marker_state = self._marker_state(publication_fd, manifest)
            if marker_state == "valid":
                return self._result(
                    manifest,
                    SubtitlePublicationState.COMPLETED,
                    published_count=len(manifest.members),
                )
            if marker_state != "absent":
                state = (
                    SubtitlePublicationState.UNSAFE
                    if marker_state == "unsafe"
                    else SubtitlePublicationState.COLLISION
                )
                return self._result(
                    manifest,
                    state,
                    reason="invalid_complete_marker",
                )

            published = 0
            for member in manifest.members:
                state = self._member_state(publication_fd, member)
                if state == "matching":
                    published += 1
                    continue
                if state != "absent":
                    return self._result(
                        manifest,
                        (
                            SubtitlePublicationState.UNSAFE
                            if state == "unsafe"
                            else SubtitlePublicationState.COLLISION
                        ),
                        published_count=published,
                        reason="member_mismatch",
                    )
                try:
                    content = await content_source.read_member(member)
                except Exception:
                    return self._result(
                        manifest,
                        SubtitlePublicationState.UNAVAILABLE,
                        published_count=published,
                        reason="content_unavailable",
                    )
                if (
                    not isinstance(content, bytes)
                    or len(content) != member.size_bytes
                    or hashlib.sha256(content).hexdigest() != member.sha256
                ):
                    return self._result(
                        manifest,
                        SubtitlePublicationState.COLLISION,
                        published_count=published,
                        reason="content_mismatch",
                    )
                try:
                    write_warning = self._write_exclusive(
                        publication_fd,
                        member.name,
                        content,
                    )
                except FileExistsError:
                    write_warning = None
                if write_warning is not None:
                    warnings.append(write_warning)
                state = self._member_state(publication_fd, member)
                if state != "matching":
                    return self._result(
                        manifest,
                        (
                            SubtitlePublicationState.UNSAFE
                            if state == "unsafe"
                            else SubtitlePublicationState.COLLISION
                        ),
                        published_count=published,
                        warnings=warnings,
                        reason="member_post_write_mismatch",
                    )
                published += 1

            try:
                marker_warning = self._write_exclusive(
                    publication_fd,
                    SUBTITLE_PUBLICATION_MARKER,
                    manifest.canonical_bytes(),
                )
            except FileExistsError:
                marker_warning = None
            if marker_warning is not None:
                warnings.append(marker_warning)
            if self._marker_state(publication_fd, manifest) != "valid":
                return self._result(
                    manifest,
                    SubtitlePublicationState.COLLISION,
                    published_count=published,
                    warnings=warnings,
                    reason="marker_post_write_mismatch",
                )
            try:
                os.fsync(publication_fd)
            except OSError:
                warnings.append("directory_fsync_unavailable")
            return self._result(
                manifest,
                SubtitlePublicationState.COMPLETED,
                published_count=published,
                warnings=warnings,
            )
        except FileExistsError:
            return self._result(
                manifest,
                SubtitlePublicationState.COLLISION,
                reason="exclusive_name_exists",
            )
        except (NotADirectoryError, PermissionError):
            return self._result(
                manifest,
                SubtitlePublicationState.UNSAFE,
                reason="unsafe_path",
            )
        except OSError:
            return self._result(
                manifest,
                SubtitlePublicationState.UNAVAILABLE,
                reason="filesystem_unavailable",
            )
        finally:
            for descriptor in (publication_fd, source_fd, root_fd):
                if descriptor is not None:
                    os.close(descriptor)

    @staticmethod
    def _result(
        manifest: SubtitlePublicationManifest,
        state: SubtitlePublicationState,
        *,
        published_count: int = 0,
        warnings: list[str] | tuple[str, ...] = (),
        reason: str | None = None,
    ) -> SubtitlePublicationResult:
        directory = (
            manifest.publication_directory
            if isinstance(manifest, SubtitlePublicationManifest)
            else ""
        )
        return SubtitlePublicationResult(
            state=state,
            publication_directory=directory,
            published_count=published_count,
            warnings=tuple(warnings),
            reason=reason,
        )

    @staticmethod
    def _open_directory(parent_fd: int, name: str) -> int:
        return os.open(
            name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )

    @classmethod
    def _open_or_create_publication(
        cls,
        source_fd: int,
        name: str,
    ) -> int:
        try:
            os.mkdir(name, 0o700, dir_fd=source_fd)
        except FileExistsError:
            pass
        return cls._open_directory(source_fd, name)

    @staticmethod
    def _unexpected_entry_state(
        publication_fd: int,
        manifest: SubtitlePublicationManifest,
    ) -> tuple[SubtitlePublicationState, str] | None:
        allowed = {item.name for item in manifest.members} | {
            SUBTITLE_PUBLICATION_MARKER
        }
        try:
            names = os.listdir(publication_fd)
        except OSError:
            return SubtitlePublicationState.UNAVAILABLE, "directory_unreadable"
        if any(name not in allowed for name in names):
            return SubtitlePublicationState.COLLISION, "unexpected_entry"
        if len({filesystem_name_key(name) for name in names}) != len(names):
            return SubtitlePublicationState.COLLISION, "casefold_collision"
        return None

    @classmethod
    def _marker_state(
        cls,
        publication_fd: int,
        manifest: SubtitlePublicationManifest,
    ) -> str:
        state, content = cls._read_regular(
            publication_fd,
            SUBTITLE_PUBLICATION_MARKER,
            expected_size=len(manifest.canonical_bytes()),
        )
        if state != "file":
            return state
        return (
            "valid"
            if content == manifest.canonical_bytes()
            else "mismatch"
        )

    @classmethod
    def _member_state(
        cls,
        publication_fd: int,
        member: SubtitlePublicationMember,
    ) -> str:
        state, content = cls._read_regular(
            publication_fd,
            member.name,
            expected_size=member.size_bytes,
        )
        if state != "file":
            return state
        assert content is not None
        if (
            len(content) == member.size_bytes
            and hashlib.sha256(content).hexdigest() == member.sha256
        ):
            return "matching"
        return "mismatch"

    @staticmethod
    def _read_regular(
        directory_fd: int,
        name: str,
        *,
        expected_size: int,
    ) -> tuple[str, bytes | None]:
        try:
            metadata = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return "absent", None
        except OSError:
            return "unsafe", None
        if not stat.S_ISREG(metadata.st_mode):
            return "unsafe", None
        if metadata.st_size != expected_size:
            return "mismatch", None
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size != expected_size
            ):
                return "unsafe", None
            content = bytearray()
            while len(content) < expected_size:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, expected_size - len(content)),
                )
                if not chunk:
                    break
                content.extend(chunk)
            after = os.fstat(descriptor)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or len(content) != expected_size
                or os.read(descriptor, 1)
            ):
                return "unsafe", None
            return "file", bytes(content)
        except OSError:
            return "unsafe", None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _write_exclusive(
        directory_fd: int,
        name: str,
        content: bytes,
    ) -> str | None:
        descriptor: int | None = None
        warning: str | None = None
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write")
                remaining = remaining[written:]
            try:
                os.fsync(descriptor)
            except OSError:
                warning = "file_fsync_unavailable"
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return warning
