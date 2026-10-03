"""조립 지점에서 받은 설정으로 Neptune 상태를 조회하는 HTTP 어댑터."""

from dataclasses import dataclass
from json import JSONDecodeError

import httpx

from app.application import GraphStatusUnavailable


@dataclass(frozen=True)
class NeptuneStatusSource:
    """Neptune의 /status 응답을 읽는 동기식 HTTP 구현.

    Attributes:
        endpoint: /status를 붙여 호출할 기본 주소.
        timeout_seconds: HTTP 클라이언트의 타임아웃 설정값. 전체 처리 시간의 상한은 아니다.
    """
    endpoint: str
    timeout_seconds: float = 5.0

    def fetch_status(self) -> object:
        """GET /status를 한 번 호출하여 JSON 해석 결과를 반환한다.

        자동 재시도하지 않으며, 실패 시에도 클라이언트를 닫는다.

        Raises:
            GraphStatusUnavailable: HTTP 요청·상태 코드 또는 JSON 해석에 실패한 경우.
        """
        try:
            # Closed even on failure. One read per check, no retry loop.
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.get(f"{self.endpoint.rstrip('/')}/status")
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            raise GraphStatusUnavailable(str(exc) or "Neptune status request failed") from exc
        except (JSONDecodeError, UnicodeDecodeError) as exc:
            raise GraphStatusUnavailable("Neptune status response is not valid JSON") from exc
