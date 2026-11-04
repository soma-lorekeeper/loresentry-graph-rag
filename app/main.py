"""환경 설정으로 의존성을 조립하고 상태 진단 HTTP 경로를 제공한다."""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app.application import (
    GraphHealthService,
    GraphStatusSource,
    GraphStatusUnavailable,
)
from app.graph import InvalidGraphStatus
from app.kafka.business_runtime import BusinessPollingRuntime
from app.kafka.runtime import FakePollingRuntime, FakePollingSettings
from app.logs import (
    AppLogger,
    LogField,
    RequestLogMiddleware,
    configure_logging,
    setup_tracing,
    skip_entry_log,
)
from app.neptune import NeptuneStatusSource

# 경로 함수가 받은 요청을 처리하는 controller 레이어다(log.layer)
LOG_LAYER = "controller"
SERVICE = "graph-rag-api"
# 상태 진단이 확인하는 데이터베이스. 주소·역할·엔진 버전은 응답에 싣지 않는다
DATABASE = "neptune"

# uvicorn이 이 모듈을 import할 때(서버가 "Started server process"를 찍기 전) 로그 형식과 trace를 맞춘다
configure_logging(SERVICE)
setup_tracing(SERVICE)


@skip_entry_log("builds the application once at startup")
def create_app(source: GraphStatusSource | None = None) -> FastAPI:
    """상태 조회 의존성을 연결한 FastAPI 앱을 생성한다.

    Args:
        source: 상태 조회 구현. 생략하면 NEPTUNE_ENDPOINT와 NEPTUNE_PORT
            환경 변수로 HTTP 구현을 구성한다. 기본값은 localhost와 8182다.

    Returns:
        서비스 확인, 생존 확인, 그래프 상태 진단 경로를 등록한 앱.
        생성 시점에는 Neptune을 호출하지 않는다. 받은 요청마다 서버 span(받은 traceparent를 잇는다)과
        접근 로그(request received·request completed)를 남긴다.
    """
    if source is None:
        host = os.getenv("NEPTUNE_ENDPOINT", "localhost")
        port = os.getenv("NEPTUNE_PORT", "8182")
        source = NeptuneStatusSource(endpoint=f"https://{host}:{port}")
    service = GraphHealthService(source)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = FakePollingSettings.from_env(os.environ)
        runtime = FakePollingRuntime(settings) if settings is not None else None
        business = BusinessPollingRuntime.from_env(os.environ)
        app.state.kafka_polling = runtime
        app.state.kafka_business = business
        try:
            if runtime is not None:
                await asyncio.to_thread(runtime.start)
            if business is not None:
                await asyncio.to_thread(business.start)
            yield
        finally:
            try:
                if business is not None:
                    await asyncio.to_thread(business.close)
            finally:
                if runtime is not None:
                    await asyncio.to_thread(runtime.close)

    app = FastAPI(title=SERVICE, lifespan=lifespan)
    # 접근 로그 미들웨어는 계측이 미들웨어 묶음 바깥에서 연 서버 span 안에서 돈다
    app.add_middleware(RequestLogMiddleware)
    FastAPIInstrumentor.instrument_app(app)

    @app.get("/")
    def root() -> dict[str, str]:
        return {"service": SERVICE}

    @app.get("/health")
    def health():
        runtime = getattr(app.state, "kafka_polling", None)
        business = getattr(app.state, "kafka_business", None)
        if (runtime is not None and not runtime.healthy) or (
            business is not None and not business.healthy
        ):
            # 503 결과 줄(request completed)에 실패 이유를 맡긴다
            AppLogger.current().add_to_outcome(
                LogField.error_type("KAFKA_WORKER_UNAVAILABLE")
            )
            return JSONResponse(
                status_code=503,
                content=_with_trace(
                    {"status": "error", "error": "Kafka polling worker unavailable"}
                ),
            )
        return {"status": "ok"}

    @app.get("/health/db")
    def health_db() -> JSONResponse:
        AppLogger.current().info("graph health check requested")
        try:
            service.check()
        except (GraphStatusUnavailable, InvalidGraphStatus) as failure:
            # 원인 줄은 상태 조회가 남겼다. 결과 줄에는 실패 종류를, 응답에는 예외 메시지 대신 고정 문구만 싣는다
            AppLogger.current().add_to_outcome(
                LogField.error_type(type(failure).__name__)
            )
            return JSONResponse(
                status_code=503,
                content=_with_trace(
                    {
                        "status": "error",
                        "service": SERVICE,
                        "database": DATABASE,
                        "error": "Graph database unavailable",
                    }
                ),
            )
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "service": SERVICE, "database": DATABASE},
        )

    return app


def _with_trace(body: dict) -> dict:
    """오류 응답 본문에 이 요청의 trace_id를 싣는다. 문의 받은 값으로 로그를 바로 찾는다."""
    trace_id = AppLogger.current().trace_id
    return body if trace_id is None else {**body, "trace_id": trace_id}


app = create_app()
