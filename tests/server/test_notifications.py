from __future__ import annotations

import pytest

from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.server.notifications import (
    ArchiveCompletedNotification,
    AttentionKind,
    AttentionNotification,
    FolderOutcome,
    NotificationContractError,
    NotificationSubject,
    NotificationType,
    PlanReadyNotification,
    RenderedNotification,
    TelegramTestNotification,
    TmdbPosterRef,
    escape_markdown_v2,
    render_notification,
)


_PLAN_HASH = "sha256:" + ("a" * 64)


def _subject(**overrides: object) -> NotificationSubject:
    values: dict[str, object] = {
        "title": "葬送的芙莉莲",
        "year": 2023,
        "work_type": TmdbWorkType.ANIME,
        "tmdb_id": 209867,
        "poster": TmdbPosterRef("/frieren_poster.jpg"),
    }
    values.update(overrides)
    return NotificationSubject(**values)  # type: ignore[arg-type]


def test_plan_ready_notification_has_a_fixed_markdown_v2_template() -> None:
    rendered = render_notification(
        PlanReadyNotification(
            subject=_subject(),
            scope_label="S01E01–E04",
            video_count=4,
            subtitle_count=4,
            unmapped_count=1,
            plan_hash=_PLAN_HASH,
        )
    )

    assert rendered.notification_type is NotificationType.PLAN_READY
    assert rendered.parse_mode == "MarkdownV2"
    assert rendered.photo_url == (
        "https://image.tmdb.org/t/p/w780/frieren_poster.jpg"
    )
    assert rendered.caption == (
        "*🧶 Reeloom · 计划待批准*\n\n"
        "*葬送的芙莉莲* \\(2023\\)\n"
        "范围：S01E01–E04\n"
        "媒体：4 视频 · 4 字幕\n"
        "未映射：1\n"
        "计划：`aaaaaaaa…`\n"
        "[TMDB · TV 209867](https://www.themoviedb.org/tv/209867)"
    )


def test_archive_completed_notification_has_a_fixed_template() -> None:
    rendered = render_notification(
        ArchiveCompletedNotification(
            subject=_subject(poster=None),
            applied_count=8,
            unmapped_count=1,
            folder_outcome=FolderOutcome.ARCHIVED,
            transaction_id="txn-7f31",
        )
    )

    assert rendered.notification_type is NotificationType.ARCHIVE_COMPLETED
    assert rendered.photo_url is None
    assert rendered.caption == (
        "*✅ Reeloom · 整理完成*\n\n"
        "*葬送的芙莉莲* \\(2023\\)\n"
        "已移动：8\n"
        "未映射：1，仍保留原位\n"
        "文件夹：已归入 archive\n"
        "事务：`txn-7f31`\n"
        "[TMDB · TV 209867](https://www.themoviedb.org/tv/209867)"
    )


def test_attention_notification_uses_enumerated_copy_not_error_text() -> None:
    rendered = render_notification(
        AttentionNotification(
            subject=_subject(poster=None),
            kind=AttentionKind.TARGET_EXISTS,
            event_id="evt-a8c2",
        )
    )

    assert rendered.notification_type is NotificationType.ATTENTION_REQUIRED
    assert rendered.caption == (
        "*⚠️ Reeloom · 需要处理*\n\n"
        "*葬送的芙莉莲* \\(2023\\)\n"
        "阶段：Preflight\n"
        "原因：目标已存在\n"
        "结果：未覆盖目标，源内容保持不变\n"
        "下一步：请在 Reeloom 中审查或恢复\n"
        "事件：`evt-a8c2`\n"
        "[TMDB · TV 209867](https://www.themoviedb.org/tv/209867)"
    )


def test_attention_notification_rejects_open_ended_kind() -> None:
    with pytest.raises(NotificationContractError) as raised:
        AttentionNotification(
            subject=_subject(),
            kind="target_exists",  # type: ignore[arg-type]
            event_id="evt-a8c2",
        )

    assert raised.value.code == "invalid_attention_kind"


def test_test_notification_is_fixed_and_has_no_user_template() -> None:
    rendered = render_notification(TelegramTestNotification())

    assert rendered.notification_type is NotificationType.TEST
    assert rendered.photo_url is None
    assert rendered.caption == (
        "*🧪 Reeloom · Telegram 测试*\n\n"
        "这是一条测试通知"
    )


def test_renderer_rejects_open_ended_payload_variants() -> None:
    with pytest.raises(NotificationContractError) as raised:
        render_notification(object())  # type: ignore[arg-type]

    assert raised.value.code == "unsupported_notification_type"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"notification_type": "test"}, "invalid_notification_type"),
        ({"caption": 1}, "invalid_caption"),
        ({"poster": "https://example.com/a.jpg"}, "invalid_poster_ref"),
    ],
)
def test_rendered_notification_contract_is_closed(
    overrides: dict[str, object],
    code: str,
) -> None:
    values: dict[str, object] = {
        "notification_type": NotificationType.TEST,
        "caption": "fixed",
        "poster": None,
    }
    values.update(overrides)

    with pytest.raises(NotificationContractError) as raised:
        RenderedNotification(**values)  # type: ignore[arg-type]

    assert raised.value.code == code


def test_markdown_v2_escapes_all_reserved_characters() -> None:
    assert escape_markdown_v2("_ * [ ] ( ) ~ ` > # + - = | { } . ! \\") == (
        r"\_ \* \[ \] \( \) \~ \` \> \# \+ \- \= \| \{ \} \. \! \\"
    )


def test_untrusted_title_cannot_inject_markdown_links_or_controls() -> None:
    rendered = render_notification(
        PlanReadyNotification(
            subject=_subject(
                title="Bad_[click](tg://user?id=1) *boom*\x00X\u2028伪造",
                poster=None,
            ),
            scope_label="S01_[all]\u2029已移动",
            video_count=1,
            subtitle_count=0,
            unmapped_count=0,
            plan_hash=_PLAN_HASH,
        )
    )

    assert r"Bad\_\[click\]\(tg://user?id\=1\) \*boom\*�X�伪造" in rendered.caption
    assert r"S01\_\[all\]�已移动" in rendered.caption
    assert "[click](tg://" not in rendered.caption
    assert "\u2028" not in rendered.caption
    assert "\u2029" not in rendered.caption


@pytest.mark.parametrize(
    "value",
    [
        "https://image.tmdb.org/t/p/original/a.jpg",
        "/nested/a.jpg",
        "/../a.jpg",
        "/a.png",
        "/a",
        "a.jpg",
    ],
)
def test_poster_ref_rejects_urls_paths_and_non_tmdb_shapes(value: str) -> None:
    with pytest.raises(NotificationContractError) as raised:
        TmdbPosterRef(value)

    assert raised.value.code == "invalid_poster_ref"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"year": 999}, "invalid_year"),
        ({"tmdb_id": 0}, "invalid_tmdb_id"),
        ({"tmdb_id": 1 << 31}, "invalid_tmdb_id"),
        ({"work_type": "anime"}, "invalid_work_type"),
    ],
)
def test_subject_validation_fails_closed(
    overrides: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(NotificationContractError) as raised:
        _subject(**overrides)

    assert raised.value.code == code


def test_subject_accepts_largest_supported_tmdb_id() -> None:
    assert _subject(tmdb_id=(1 << 31) - 1).tmdb_id == (1 << 31) - 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"video_count": -1},
        {"subtitle_count": True},
        {"unmapped_count": 1_000_001},
        {"plan_hash": "a" * 64},
        {"scope_label": "x" * 100},
    ],
)
def test_plan_payload_validation_fails_closed(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "subject": _subject(),
        "scope_label": "S01",
        "video_count": 1,
        "subtitle_count": 1,
        "unmapped_count": 0,
        "plan_hash": _PLAN_HASH,
    }
    values.update(overrides)

    with pytest.raises(NotificationContractError):
        PlanReadyNotification(**values)  # type: ignore[arg-type]


def test_title_is_normalized_bounded_and_caption_stays_below_photo_limit() -> None:
    subject = _subject(title=("Ａ" * 400), year=None, tmdb_id=None, poster=None)
    rendered = render_notification(
        PlanReadyNotification(
            subject=subject,
            scope_label="整季",
            video_count=1_000_000,
            subtitle_count=1_000_000,
            unmapped_count=1_000_000,
            plan_hash=_PLAN_HASH,
        )
    )

    assert subject.title == "A" * 240
    assert len(rendered.caption.encode("utf-8")) <= 900
