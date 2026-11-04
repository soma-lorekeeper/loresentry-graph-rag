"""여러 테스트가 함께 쓰는 로그 줄 캡처. 운영과 같은 ECS 포매터로 만든 JSON 줄을 읽는다."""

import io
import json
import logging

import pytest

from app.logs import setup_tracing
from app.logs._format import EcsFormatter

# 운영 시작점(app.main import)과 같이 SDK TracerProvider를 둔다. 테스트 파일 하나만 돌려도 span ID가 생긴다
setup_tracing("graph-rag-api")


class CapturedLines:
    """테스트 동안 root logger가 받은 줄. 운영 포매터가 만든 JSON 그대로다."""

    def __init__(self, stream: io.StringIO):
        self._stream = stream

    def text(self) -> str:
        """줄 전체의 원문. 비밀 값이 어디에도 없는지 볼 때 쓴다."""
        return self._stream.getvalue()

    def documents(self) -> list[dict]:
        """줄마다 중첩 JSON 객체."""
        return [json.loads(line) for line in self.text().splitlines() if line]

    def __call__(self) -> list[dict]:
        """줄마다 점 이름으로 편 dict({"log.level": "INFO", ...})."""
        return [flatten(document) for document in self.documents()]

    def find(self, message: str) -> list[dict]:
        """문구가 같은 줄들(점 이름으로 편 dict)."""
        return [line for line in self() if line["message"] == message]

    def messages(self) -> list[str]:
        return [line["message"] for line in self()]


def flatten(document: dict, prefix: str = "") -> dict:
    """중첩 객체를 점 이름 dict로 편다. 배열(tags)은 그대로 둔다."""
    flat = {}
    for key, value in document.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, name + "."))
        else:
            flat[name] = value
    return flat


@pytest.fixture
def log_lines():
    """root logger에 운영 포매터를 단 handler를 붙여 이 테스트의 줄을 모은다."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(EcsFormatter("graph-rag-api", "test-version"))
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield CapturedLines(stream)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
