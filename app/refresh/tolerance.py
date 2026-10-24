"""모델 출력의 기계적인 실수를 실패 대신 서버가 바로잡는 규칙."""

import re
from difflib import SequenceMatcher

_EDITORIAL = re.compile(
    "|".join(
        (
            r"(단정|추정|덧붙이|보충)(하|되)?지\s*않는다",
            r"(추가|기록|서술|작성|명시)하지\s*않는다",
            r"원고(에|에서|로|상)\s*(없|확인|나오|드러나|명시|제시|언급)",
            r"만\s*(기록|서술|반영)(한다|하며|하고)",
            r"(기존|이전)\s*(내용|문장|문단|서술)",
            r"(삭제|정정)(한다|된다|되며|하며)",
            r"확인되지\s*않는다",
            r"(확인|확정)된\s*것은\s*아니",
            r"(이력|사실|내용)은\s*(그대로다|유지된다)",
            r"추가\s*내용",
        )
    )
)
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")


def resolve_id(value, known):
    """옮겨 적다 몇 글자 틀린 문서 ID를 가장 가까운 실제 ID 하나로 고친다. 애매하면 None이다."""
    if not isinstance(value, str):
        return None
    if value in known:
        return value
    scored = sorted(
        (
            (SequenceMatcher(None, value, key, autojunk=False).ratio(), key)
            for key in known
            if isinstance(key, str)
        ),
        reverse=True,
    )
    if scored and scored[0][0] >= 0.85:
        if len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1:
            return scored[0][1]
    return None


def strip_editorial(text):
    """작성 지시·확인 단서가 본문 문장으로 옮겨진 경우 그 문장만 지운다.

    지울 문장이 없으면 원문을 그대로, 전부 지워지면 원문을 돌려준다.
    """
    lines, removed = [], False
    for line in text.split("\n"):
        if not line.strip():
            lines.append(line)
            continue
        sentences = _SENTENCE_END.split(line.strip())
        kept = [s for s in sentences if not _EDITORIAL.search(s)]
        if len(kept) != len(sentences):
            removed = True
            if not kept:
                continue
            line = " ".join(kept)
        lines.append(line)
    if not removed:
        return text
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return cleaned or text
