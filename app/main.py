from fastapi import FastAPI

app = FastAPI(title="graph-rag-api")


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "graph-rag-api"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
