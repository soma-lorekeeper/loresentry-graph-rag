"""설정이나 외부 IO에 접근하지 않는 그래프 상태 응답 해석 규칙."""

from dataclasses import dataclass


class InvalidGraphStatus(ValueError):
    """진단용 상태 스냅샷으로 해석할 수 없는 응답 구조."""


@dataclass(frozen=True)
class GraphStatus:
    """상태 응답에서 추출한 역할과 엔진·Gremlin 버전."""
    role: str
    engine_version: str
    gremlin_version: str


def interpret_status(payload: object) -> GraphStatus:
    """원본 응답을 진단용 메타데이터로 해석한다.

    Args:
        payload: JSON 해석 결과. 객체 형태여야 하며 추가 필드는 무시한다.

    Returns:
        역할과 엔진·Gremlin 버전. 없는 메타데이터는 unknown으로 표현한다.
        쿼리·쓰기 준비 상태를 판단한 결과는 아니다.

    Raises:
        InvalidGraphStatus: 객체 구조나 메타데이터 값의 타입이 잘못된 경우.
    """
    if not isinstance(payload, dict):
        raise InvalidGraphStatus("Graph status must be a JSON object")
    gremlin = payload.get("gremlin", {})
    if not isinstance(gremlin, dict):
        raise InvalidGraphStatus("Graph status gremlin must be a JSON object")
    return GraphStatus(
        role=_metadata(payload, "role"),
        engine_version=_metadata(payload, "dbEngineVersion"),
        gremlin_version=_metadata(gremlin, "version"),
    )


def _metadata(payload: dict, key: str) -> str:
    value = payload.get(key, "unknown")
    if not isinstance(value, str):
        raise InvalidGraphStatus(f"Graph status {key} must be a string")
    return value
