"""Validated, network-free presentation contracts for outbound notifications."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from reeloom.kernel.tmdb import TmdbWorkType

_MAX_TITLE_BYTES = 240
_MAX_SCOPE_BYTES = 80
_MAX_CAPTION_BYTES = 900
_MAX_COUNT = 1_000_000
_MAX_TMDB_ID = (1 << 31) - 1
_PLAN_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_POSTER_REF_PATTERN = re.compile(
    r"^/[A-Za-z0-9_-]{1,200}\.(?:jpg|jpeg)$",
    re.IGNORECASE,
)
_MARKDOWN_V2_RESERVED = frozenset("_*[]()~`>#+-=|{}.!\\")
_TMDB_POSTER_BASE = "https://image.tmdb.org/t/p/w780"


class NotificationContractError(ValueError):
    """A fail-closed validation or rendering error with a stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NotificationType(StrEnum):
    PLAN_READY = "plan_ready"
    ARCHIVE_COMPLETED = "archive_completed"
    ATTENTION_REQUIRED = "attention_required"
    TEST = "test"


class FolderOutcome(StrEnum):
    ARCHIVED = "archived"
    REMOVED_EMPTY = "removed_empty"
    NOT_APPLICABLE = "not_applicable"


class AttentionKind(StrEnum):
    TARGET_EXISTS = "target_exists"
    SOURCE_CHANGED = "source_changed"
    APPROVAL_EXPIRED = "approval_expired"
    EXECUTION_ROLLED_BACK = "execution_rolled_back"
    EXECUTION_INTERRUPTED = "execution_interrupted"
    FOLDER_DISPOSITION_FAILED = "folder_disposition_failed"


_ATTENTION_COPY = {
    AttentionKind.TARGET_EXISTS: (
        "Preflight", "目标已存在", "未覆盖目标，源内容保持不变"
    ),
    AttentionKind.SOURCE_CHANGED: (
        "Preflight", "源文件状态已变化", "未覆盖目标，源内容保持不变"
    ),
    AttentionKind.APPROVAL_EXPIRED: ("Preflight", "批准已过期", "源内容保持不变"),
    AttentionKind.EXECUTION_ROLLED_BACK: (
        "Execution", "执行失败", "已回滚已完成的移动"
    ),
    AttentionKind.EXECUTION_INTERRUPTED: (
        "Recovery", "执行被中断", "已停止执行，需要恢复处理"
    ),
    AttentionKind.FOLDER_DISPOSITION_FAILED: (
        "Folder", "源文件夹收尾失败", "媒体结果不变，需要处理文件夹"
    ),
}


@dataclass(frozen=True, slots=True)
class TmdbPosterRef:
    """An opaque TMDB poster path, never a caller-controlled URL or file path."""

    value: str

    def __post_init__(self) -> None:
        _require(
            isinstance(self.value, str)
            and _POSTER_REF_PATTERN.fullmatch(self.value) is not None,
            "invalid_poster_ref",
        )

    @property
    def url(self) -> str:
        return f"{_TMDB_POSTER_BASE}{self.value}"


@dataclass(frozen=True, slots=True)
class NotificationSubject:
    title: str
    year: int | None
    work_type: TmdbWorkType
    tmdb_id: int | None
    poster: TmdbPosterRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "title",
            _bounded_text(
                self.title,
                max_bytes=_MAX_TITLE_BYTES,
                code="invalid_title",
            ),
        )
        _require(
            self.year is None
            or type(self.year) is int
            and 1000 <= self.year <= 9999,
            "invalid_year",
        )
        _require(isinstance(self.work_type, TmdbWorkType), "invalid_work_type")
        _require(
            self.tmdb_id is None
            or type(self.tmdb_id) is int
            and 1 <= self.tmdb_id <= _MAX_TMDB_ID,
            "invalid_tmdb_id",
        )
        _require(
            self.poster is None
            or isinstance(self.poster, TmdbPosterRef),
            "invalid_poster_ref",
        )


@dataclass(frozen=True, slots=True)
class PlanReadyNotification:
    subject: NotificationSubject
    scope_label: str
    video_count: int
    subtitle_count: int
    unmapped_count: int
    plan_hash: str

    def __post_init__(self) -> None:
        _validate_subject(self.subject)
        object.__setattr__(
            self,
            "scope_label",
            _bounded_text(
                self.scope_label,
                max_bytes=_MAX_SCOPE_BYTES,
                code="invalid_scope_label",
                truncate=False,
            ),
        )
        _validate_count(self.video_count)
        _validate_count(self.subtitle_count)
        _validate_count(self.unmapped_count)
        _require(
            isinstance(self.plan_hash, str)
            and _PLAN_HASH_PATTERN.fullmatch(self.plan_hash) is not None,
            "invalid_plan_hash",
        )


@dataclass(frozen=True, slots=True)
class ArchiveCompletedNotification:
    subject: NotificationSubject
    applied_count: int
    unmapped_count: int
    folder_outcome: FolderOutcome
    transaction_id: str

    def __post_init__(self) -> None:
        _validate_subject(self.subject)
        _validate_count(self.applied_count)
        _validate_count(self.unmapped_count)
        _require(
            isinstance(self.folder_outcome, FolderOutcome),
            "invalid_folder_outcome",
        )
        _validate_opaque_id(self.transaction_id, code="invalid_transaction_id")


@dataclass(frozen=True, slots=True)
class AttentionNotification:
    subject: NotificationSubject
    kind: AttentionKind
    event_id: str

    def __post_init__(self) -> None:
        _validate_subject(self.subject)
        _require(isinstance(self.kind, AttentionKind), "invalid_attention_kind")
        _validate_opaque_id(self.event_id, code="invalid_event_id")


@dataclass(frozen=True, slots=True)
class TelegramTestNotification:
    """A deliberately field-free test intent: callers cannot inject a template."""


NotificationPayload: TypeAlias = (
    PlanReadyNotification
    | ArchiveCompletedNotification
    | AttentionNotification
    | TelegramTestNotification
)


@dataclass(frozen=True, slots=True)
class RenderedNotification:
    notification_type: NotificationType
    caption: str
    poster: TmdbPosterRef | None

    def __post_init__(self) -> None:
        _require(
            isinstance(self.notification_type, NotificationType),
            "invalid_notification_type",
        )
        _require(
            isinstance(self.caption, str)
            and bool(self.caption)
            and len(self.caption.encode("utf-8")) <= _MAX_CAPTION_BYTES,
            "invalid_caption",
        )
        _require(
            self.poster is None or isinstance(self.poster, TmdbPosterRef),
            "invalid_poster_ref",
        )

    @property
    def parse_mode(self) -> str:
        return "MarkdownV2"

    @property
    def photo_url(self) -> str | None:
        return self.poster.url if self.poster is not None else None


def escape_markdown_v2(value: str) -> str:
    """Escape Telegram MarkdownV2 outside pre/code and link destinations."""

    if not isinstance(value, str):
        raise NotificationContractError("invalid_markdown_text")
    return "".join(
        f"\\{character}" if character in _MARKDOWN_V2_RESERVED else character
        for character in value
    )


def render_notification(payload: NotificationPayload) -> RenderedNotification:
    """Render one of the closed notification payload variants."""

    if type(payload) is PlanReadyNotification:
        return _render_plan_ready(payload)
    if type(payload) is ArchiveCompletedNotification:
        return _render_archive_completed(payload)
    if type(payload) is AttentionNotification:
        return _render_attention(payload)
    if type(payload) is TelegramTestNotification:
        return _render_test()
    raise NotificationContractError("unsupported_notification_type")


def _render_plan_ready(payload: PlanReadyNotification) -> RenderedNotification:
    lines = [
        "*🧶 Reeloom · 计划待批准*",
        "",
        _subject_heading(payload.subject),
        f"范围：{escape_markdown_v2(payload.scope_label)}",
        f"媒体：{payload.video_count} 视频 · {payload.subtitle_count} 字幕",
        f"未映射：{payload.unmapped_count}",
        f"计划：{_inline_code(payload.plan_hash.removeprefix('sha256:')[:8] + '…')}",
    ]
    return _rendered(NotificationType.PLAN_READY, payload.subject, lines)


def _render_archive_completed(
    payload: ArchiveCompletedNotification,
) -> RenderedNotification:
    folder_copy = {
        FolderOutcome.ARCHIVED: "已归入 archive",
        FolderOutcome.REMOVED_EMPTY: "空目录已移除",
        FolderOutcome.NOT_APPLICABLE: "无需处理",
    }[payload.folder_outcome]
    lines = [
        "*✅ Reeloom · 整理完成*",
        "",
        _subject_heading(payload.subject),
        f"已移动：{payload.applied_count}",
        f"未映射：{payload.unmapped_count}，仍保留原位",
        f"文件夹：{folder_copy}",
        f"事务：{_inline_code(payload.transaction_id)}",
    ]
    return _rendered(NotificationType.ARCHIVE_COMPLETED, payload.subject, lines)


def _render_attention(payload: AttentionNotification) -> RenderedNotification:
    stage_copy, reason_copy, effect_copy = _ATTENTION_COPY[payload.kind]
    lines = [
        "*⚠️ Reeloom · 需要处理*",
        "",
        _subject_heading(payload.subject),
        f"阶段：{stage_copy}",
        f"原因：{reason_copy}",
        f"结果：{effect_copy}",
        "下一步：请在 Reeloom 中审查或恢复",
        f"事件：{_inline_code(payload.event_id)}",
    ]
    return _rendered(NotificationType.ATTENTION_REQUIRED, payload.subject, lines)


def _render_test() -> RenderedNotification:
    return RenderedNotification(
        notification_type=NotificationType.TEST,
        caption="*🧪 Reeloom · Telegram 测试*\n\n这是一条测试通知",
        poster=None,
    )


def _rendered(
    notification_type: NotificationType,
    subject: NotificationSubject,
    lines: list[str],
) -> RenderedNotification:
    _append_tmdb_link(lines, subject)
    return RenderedNotification(
        notification_type=notification_type,
        caption="\n".join(lines),
        poster=subject.poster,
    )


def _subject_heading(subject: NotificationSubject) -> str:
    title = escape_markdown_v2(subject.title)
    if subject.year is None:
        return f"*{title}*"
    return f"*{title}* \\({subject.year}\\)"


def _append_tmdb_link(lines: list[str], subject: NotificationSubject) -> None:
    if subject.tmdb_id is None:
        return
    media_type = subject.work_type.media_type.value
    lines.append(
        f"[TMDB · {media_type.upper()} {subject.tmdb_id}]"
        f"(https://www.themoviedb.org/{media_type}/{subject.tmdb_id})"
    )


def _inline_code(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("`", "\\`")
    return f"`{escaped}`"


def _bounded_text(
    value: object,
    *,
    max_bytes: int,
    code: str,
    truncate: bool = True,
) -> str:
    if not isinstance(value, str):
        raise NotificationContractError(code)
    normalized = unicodedata.normalize("NFKC", value)
    visible = "".join(
        character if unicodedata.category(character)[:1] not in {"C", "Z"}
        or character == " "
        else "\N{REPLACEMENT CHARACTER}"
        for character in normalized
    ).strip()
    if not visible:
        raise NotificationContractError(code)
    encoded = visible.encode("utf-8")
    if len(encoded) > max_bytes:
        if not truncate:
            raise NotificationContractError(code)
        visible = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return visible


def _validate_subject(value: object) -> None:
    if not isinstance(value, NotificationSubject):
        raise NotificationContractError("invalid_subject")


def _validate_count(value: object) -> None:
    if type(value) is not int or not 0 <= value <= _MAX_COUNT:
        raise NotificationContractError("invalid_count")


def _validate_opaque_id(value: object, *, code: str) -> None:
    _require(
        isinstance(value, str) and _OPAQUE_ID_PATTERN.fullmatch(value) is not None,
        code,
    )


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise NotificationContractError(code)
