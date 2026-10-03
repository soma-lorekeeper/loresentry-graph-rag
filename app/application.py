"""그래프 상태 조회 IO와 순수 해석을 연결하는 애플리케이션 계층."""

from dataclasses import dataclass
from typing import Protocol

from app.graph import GraphStatus, interpret_status


class GraphStatusUnavailable(Exception):
    """상태 응답을 가져오거나 JSON으로 해석할 수 없는 IO 실패."""


class GraphStatusSource(Protocol):
    """상태 진단용 원본 응답을 한 번 읽는 IO 계약."""
    @property
    def endpoint(self) -> str:
        """진단 결과에 표시할 상태 조회 대상 주소."""
        ...

    def fetch_status(self) -> object:
        """상태 응답을 한 번 읽어 JSON 해석 결과를 반환한다.

        응답 구조의 업무 검증은 interpret_status가 담당한다.

        Raises:
            GraphStatusUnavailable: 통신 또는 JSON 해석 실패로 응답을 제공할 수 없는 경우.
        """
        ...


@dataclass(frozen=True)
class GraphHealth:
    """진단에 사용한 주소와 해석된 그래프 메타데이터."""
    endpoint: str
    status: GraphStatus


class GraphHealthService:
    """상태 조회 구현을 주입받아 조회 후 순수 해석을 수행하는 서비스."""
    def __init__(self, source: GraphStatusSource):
        self._source = source

    def check(self) -> GraphHealth:
        """상태를 한 번 조회하여 진단 결과를 반환한다.

        조회 성공은 그래프 쿼리·쓰기 준비 완료를 보장하지 않는다. 자동 재시도하지 않는다.

        Raises:
            GraphStatusUnavailable: 조회 또는 JSON 해석에 실패한 경우.
            InvalidGraphStatus: 응답의 메타데이터 구조가 유효하지 않은 경우.
        """
        payload = self._source.fetch_status()
        status = interpret_status(payload)
        return GraphHealth(endpoint=self._source.endpoint, status=status)
