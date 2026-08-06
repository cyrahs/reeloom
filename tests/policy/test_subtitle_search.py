from __future__ import annotations

import pytest

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.policy.subtitle_search import compile_subtitle_search_aliases


@pytest.mark.parametrize(
    ("title", "expected"),
    (
        ("测试动画", ("测试动画",)),
        (
            "我的百合乃工作是也！",
            ("我的百合乃工作是也!", "我的百合乃工作是也"),
        ),
        (
            "空之色，水之色",
            ("空之色,水之色", "空之色 水之色"),
        ),
        ("Fate/stay night", ("fate stay night",)),
        (
            "WORKING 字幕*|Group+Name",
            ("working 字幕 group name",),
        ),
    ),
)
def test_compile_subtitle_search_aliases(
    title: str,
    expected: tuple[str, ...],
) -> None:
    assert compile_subtitle_search_aliases(title) == expected


def test_compiler_rejects_low_information_relaxed_query() -> None:
    assert compile_subtitle_search_aliases("C++") == ()


@pytest.mark.parametrize("title", ("", " \t ", "bad\x00title"))
def test_compiler_rejects_invalid_title(title: str) -> None:
    with pytest.raises(DomainError) as raised:
        compile_subtitle_search_aliases(title)

    assert raised.value.code is ErrorCode.INVALID_SUBTITLE_SEARCH_DATA
