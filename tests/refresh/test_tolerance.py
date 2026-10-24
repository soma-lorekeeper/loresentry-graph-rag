"""모델 출력의 기계적 실수를 서버가 고치는 규칙."""

import pytest

from app.refresh.tolerance import resolve_id, strip_editorial

KNOWN = {
    "01a1ea26-09f2-7ee0-9d76-60ce4854a187",
    "01a1ea26-09db-7a13-8c2e-1d0f9a8b7c6d",
    "new:CHARACTER:함예린",
}


@pytest.mark.parametrize(
    "value,expected",
    [
        (
            "01a1ea26-09f2-7ee0-9d76-60ce4854a187",
            "01a1ea26-09f2-7ee0-9d76-60ce4854a187",
        ),
        (
            "01a1ea26-09f2-7ee0-0d9d-76ce4854a187",
            "01a1ea26-09f2-7ee0-9d76-60ce4854a187",
        ),
        ("new:CHARACTER:함예", "new:CHARACTER:함예린"),
        ("01a1ea26-0000-7000-0000-000000000000", None),
        (["not", "an", "id"], None),
    ],
)
def test_miscopied_ids_resolve_to_one_close_id(value, expected):
    assert resolve_id(value, KNOWN) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "흑풍회의 회주다. 확인되지 않은 구성·목적·활동은 덧붙이지 않는다.",
            "흑풍회의 회주다.",
        ),
        ("묵린은 사라졌다. 이후의 행방이나 정체는 원고에 없다.", "묵린은 사라졌다."),
        (
            "알리바이가 확인됐다. 따라서 집에서 잤다는 진술은 정정한다.",
            "알리바이가 확인됐다.",
        ),
        ("검이다. 그 검의 실제 소유가 확정된 것은 아니다.", "검이다."),
        ("첫 줄이다.\n\n이후 능력 변화는 확인되지 않는다.", "첫 줄이다."),
    ],
)
def test_editorial_sentences_are_removed(text, expected):
    assert strip_editorial(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "왕비의 승하는 공식적으로 확인되지 않았다.",
        "사관은 매일 왕의 언행을 기록한다.",
        "지우는 출판사에서 원고를 검토하던 편집자였다.",
        "그는 과거를 이야기하지 않는다.",
        "실제 늙은 고양이라고 단정하지 않는다.",
    ],
)
def test_in_world_sentences_and_fully_editorial_bodies_are_kept(text):
    assert strip_editorial(text) == text
