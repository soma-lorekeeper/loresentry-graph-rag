"""받은 HTTP 요청 하나의 시작점. 요청마다 AppLogger를 만들어 바인딩하고 접근 로그를 남긴다(LOG_SCHEMA.md 5절 "접근 로그").

content RequestLogFilter.java와 같은 줄·필드다. 순수 ASGI 미들웨어라 스트리밍 응답(SSE)도 본문이 끝날 때까지 기다린다.

순서: OpenTelemetry FastAPI 계측이 미들웨어 묶음 바깥에서 서버 span을 연다(받은 traceparent가 있으면 그 trace를
잇는다). 이 미들웨어는 그 안쪽(app.add_middleware)에서 돈다. 그래서 이 요청의 모든 줄이 보낸 쪽과 같은 trace.id를 갖는다.

하는 일
1. 지금 span의 trace·span ID와 gateway가 확인해 보낸 사용자(X-User-Id, UUID일 때만)로 로그 객체를 만든다. 요청의
   모든 줄에 받은 경로(url.path), 경로 템플릿(http.route, 맞는 경로가 없는 404에는 없음), 메서드(http.request.method)를
   붙인다. /health, /health/... 요청의 모든 줄에는 tags: health가 붙어 정상(info) 줄은 Logstash가 버린다.
2. `request received`와 `request completed`(응답 상태·걸린 시간·맡겨진 결과 필드)를 남긴다. 레벨은 5xx error,
   4xx warn, 그 외 info다. 응답 본문이 끝날 때(또는 끊길 때) 결과 줄을 쓴다.
3. 처리되지 않은 예외는 결과 줄 대신 `request failed with an unhandled exception`(error, error.type, 메시지 없는
   스택, 상태 500)을 남기고 예외를 그대로 다시 던진다.

쿼리스트링은 남기지 않는다. 경로만 url.path에 넣는다.
"""

import logging
import time
from uuid import UUID

from starlette.routing import Match

from app.logs._fields import LogField, duration
from app.logs._logger import bind, internal_origin, new_logger, reset
from app.logs._trace import current_ids

# gateway가 세션을 확인한 뒤 넣는 사용자 헤더
_USER_HEADER = b"x-user-id"
# 접근 로그 줄의 위치. 우리 서비스 코드가 아니라 웹 서버가 부르는 필터 줄이라 위치가 이 미들웨어다
_ORIGIN = internal_origin(__name__, "__call__", "filter")


class RequestLogMiddleware:
    """요청 하나마다 로그 객체를 바인딩하고 시작·결과 줄을 남기는 ASGI 미들웨어. `app.add_middleware`로 단다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        logger = new_logger(*current_ids(), _user(scope.get("headers", ())))._derive(
            LogField.url_path(path),
            LogField.route(_route(scope)),
            LogField.http_method(scope.get("method")),
        )
        if path == "/health" or path.startswith("/health/"):
            logger = logger._tagged("health")
        token = bind(logger)
        started = time.perf_counter_ns()
        # 응답 상태와 결과 줄을 썼는지. send를 감싸 응답 시작·마지막 본문을 본다
        state = {"status": None, "done": False}

        def finish(level_status: int | None, failure: BaseException | None) -> None:
            """결과 줄 하나. 한 요청에 한 번만 쓴다."""
            if state["done"]:
                return
            state["done"] = True
            elapsed = duration((time.perf_counter_ns() - started) / 1_000_000_000)
            outcome = logger._outcome_fields()
            if failure is not None:
                # 같은 이름은 먼저 넣은 값이 남으므로 error.type을 맡겨진 결과 필드보다 앞에 둔다
                logger._emit(
                    logging.ERROR,
                    "request failed with an unhandled exception",
                    (
                        LogField.error_type(type(failure).__name__),
                        LogField.safe_stack_trace(failure),
                        LogField.status_code(500),
                        elapsed,
                        *outcome,
                    ),
                    _ORIGIN,
                )
                return
            fields = (LogField.status_code(level_status), elapsed, *outcome)
            if level_status is not None and level_status >= 500:
                logger._emit(logging.ERROR, "request completed", fields, _ORIGIN)
            elif level_status is not None and level_status >= 400:
                logger._emit(logging.WARNING, "request completed", fields, _ORIGIN)
            else:
                logger._emit(logging.INFO, "request completed", fields, _ORIGIN)

        async def tracked_send(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
            await send(message)
            # 마지막 본문 조각을 보낸 뒤가 요청의 끝이다(스트리밍 응답은 여러 조각이다)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                finish(state["status"], None)

        try:
            logger._emit(logging.INFO, "request received", (), _ORIGIN)
            try:
                await self.app(scope, receive, tracked_send)
            except Exception as failure:
                # 처리되지 않은 예외. 예외 메시지에는 요청 값이 섞일 수 있어 클래스 이름과 메시지 없는 스택만 남긴다
                finish(None, failure)
                raise
        finally:
            # 본문이 끝나기 전에 끊긴 요청(연결 끊김, 취소)도 그때까지의 상태로 결과 줄을 남긴다
            finish(state["status"], None)
            reset(token)


def _user(headers) -> UUID | None:
    """gateway가 보낸 사용자. 값이 하나이고 표준 표기 UUID일 때만 쓴다. 헤더가 없는 요청(헬스체크 등)은 None."""
    values = [value for name, value in headers if name.lower() == _USER_HEADER]
    if len(values) != 1 or len(values[0]) != 36:
        return None
    try:
        return UUID(values[0].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None


def _route(scope) -> str | None:
    """앱이 이 요청에 고를 경로 템플릿(/health/db 등). 맞는 경로가 없거나(404) 메서드가 다르면(405) None.

    Starlette가 나중에 같은 경로 목록으로 똑같이 고르므로 결과가 같다. 매칭은 scope를 바꾸지 않는다.
    """
    app = scope.get("app")
    router = getattr(app, "router", app)
    for route in getattr(router, "routes", ()):
        try:
            match, _ = route.matches(scope)
        except Exception:
            # 매칭 중 예외가 나는 경로는 템플릿 없이 둔다
            continue
        if match == Match.FULL:
            return getattr(route, "path", None)
    return None
