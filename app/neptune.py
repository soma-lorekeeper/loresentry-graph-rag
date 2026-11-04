"""조립 지점에서 받은 설정으로 Neptune 상태를 조회하는 HTTP 어댑터."""

from dataclasses import dataclass
from json import JSONDecodeError

import httpx

from app.application import GraphStatusUnavailable
from app.logs import AppLogger, LogField

# Neptune 상태 API를 부르는 client 레이어다(log.layer)
LOG_LAYER = "client"


@dataclass(frozen=True)
class NeptuneStatusSource:
    """Neptune의 /status 응답을 읽는 동기식 HTTP 구현.

    Attributes:
        endpoint: /status를 붙여 호출할 기본 주소.
        timeout_seconds: HTTP 클라이언트의 타임아웃 설정값. 전체 처리 시간의 상한은 아니다.
    """

    endpoint: str
    timeout_seconds: float = 10.0

    def fetch_status(self) -> object:
        """GET /status를 한 번 호출하여 JSON 해석 결과를 반환한다.

        자동 재시도하지 않으며, 실패 시에도 클라이언트를 닫는다.

        Raises:
            GraphStatusUnavailable: HTTP 요청·상태 코드 또는 JSON 해석에 실패한 경우.
        """
        # Neptune은 데이터베이스라 호출마다 결과 줄을 남기지 않는다. 진입 줄과 실패 줄만 남긴다(주소는 남기지 않는다)
        AppLogger.current().info("neptune status get call")
        try:
            # Closed even on failure. One read per check, no retry loop.
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.get(f"{self.endpoint.rstrip('/')}/status")
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            # 상대가 답한 실패(HTTPStatusError)면 응답 상태도 남긴다
            status = getattr(getattr(exc, "response", None), "status_code", None)
            AppLogger.current().warn(
                "neptune status call failed",
                LogField.error_type(type(exc).__name__),
                LogField.status_code(status),
            )
            raise GraphStatusUnavailable(
                str(exc) or "Neptune status request failed"
            ) from exc
        except (JSONDecodeError, UnicodeDecodeError) as exc:
            AppLogger.current().warn(
                "neptune status call failed", LogField.error_type(type(exc).__name__)
            )
            raise GraphStatusUnavailable(
                "Neptune status response is not valid JSON"
            ) from exc
