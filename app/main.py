"""환경 설정으로 의존성을 조립하고 상태 진단 HTTP 경로를 제공한다."""

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.application import GraphHealthService, GraphStatusSource, GraphStatusUnavailable
from app.graph import InvalidGraphStatus
from app.neptune import NeptuneStatusSource

SERVICE = "graph-rag-api"


def create_app(source: GraphStatusSource | None = None) -> FastAPI:
    """상태 조회 의존성을 연결한 FastAPI 앱을 생성한다.

    Args:
        source: 상태 조회 구현. 생략하면 NEPTUNE_ENDPOINT와 NEPTUNE_PORT
            환경 변수로 HTTP 구현을 구성한다. 기본값은 localhost와 8182다.

    Returns:
        서비스 확인, 생존 확인, 그래프 상태 진단 경로를 등록한 앱.
        생성 시점에는 Neptune을 호출하지 않는다.
    """
    if source is None:
        host = os.getenv("NEPTUNE_ENDPOINT", "localhost")
        port = os.getenv("NEPTUNE_PORT", "8182")
        source = NeptuneStatusSource(endpoint=f"https://{host}:{port}")
    service = GraphHealthService(source)
    app = FastAPI(title=SERVICE)

    @app.get("/")
    def root() -> dict[str, str]:
        return {"service": SERVICE}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/db")
    def health_db() -> JSONResponse:
        try:
            result = service.check()
        except (GraphStatusUnavailable, InvalidGraphStatus) as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "error", "service": SERVICE, "error": str(exc)},
            )
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok", "service": SERVICE, "endpoint": result.endpoint,
                "role": result.status.role,
                "dbEngineVersion": result.status.engine_version,
                "gremlin": result.status.gremlin_version,
            },
        )

    return app


app = create_app()
