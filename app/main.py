from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import graph

app = FastAPI(title="graph-rag-api")

SERVICE = "graph-rag-api"


@app.get("/")
def root() -> dict[str, str]:
    return {"service": SERVICE}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/db")
def health_db() -> JSONResponse:
    try:
        result = graph.check()
    except Exception as exception:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "service": SERVICE, "error": str(exception)},
        )

    return JSONResponse(
        status_code=200,
        content={"status": "ok", "service": SERVICE, **result},
    )
