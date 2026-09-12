import os

import httpx

REQUEST_TIMEOUT_SECONDS = 5.0


def endpoint() -> str:
    host = os.getenv("NEPTUNE_ENDPOINT", "localhost")
    port = os.getenv("NEPTUNE_PORT", "8182")
    return f"https://{host}:{port}"


def check() -> dict[str, str]:
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = client.get(f"{endpoint()}/status")
        response.raise_for_status()
        payload = response.json()

    return {
        "endpoint": endpoint(),
        "role": payload.get("role", "unknown"),
        "dbEngineVersion": payload.get("dbEngineVersion", "unknown"),
        "gremlin": payload.get("gremlin", {}).get("version", "unknown"),
    }
