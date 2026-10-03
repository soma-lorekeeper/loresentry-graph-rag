"""실제 SDK와 loopback 서버 간 검증. 원격 API와 실제 키를 사용하지 않는다."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from openai import OpenAI

from app.adapters.llm import ModelLimits, OpenAIProposalModel
from app.refresh.errors import RefreshFailure
from app.refresh.models import ModelInput, Usage

pytestmark = pytest.mark.integration


def response_payload():
    return {
        "id": "resp_local",
        "object": "response",
        "created_at": 1,
        "model": "gpt-5.6-luna",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "parallel_tool_calls": False,
        "tools": [],
        "tool_choice": "auto",
        "temperature": 1,
        "top_p": 1,
        "output": [
            {
                "id": "msg_local",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "annotations": [],
                        "text": json.dumps(
                            {"document_proposals": [], "relation_proposals": []}
                        ),
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "input_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 15},
        },
    }


@pytest.fixture
def server():
    state = {"status": 200, "payload": response_payload(), "delay": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            state["requests"].append(
                (
                    self.path,
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            time.sleep(state["delay"])
            body = json.dumps(state["payload"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-request-id", "local-request")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{httpd.server_port}/v1"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def invoke(url, timeout=2):
    client = OpenAI(api_key="local-test-only", base_url=url, max_retries=0)
    try:
        with client:
            result = OpenAIProposalModel(
                client, ModelLimits("gpt-5.6-luna", timeout, 20000, 2000, 22000)
            ).generate(
                ModelInput(
                    '{"text":"한글 🐈"}',
                    "refresh-candidate-v1",
                    "refresh-prompt-v2",
                    "gpt-5.6-luna",
                    instructions="Trusted instructions",
                )
            )
        return result
    finally:
        assert client.is_closed()


def test_sdk_wire_format_usage_and_lifecycle(server):
    state, url = server
    assert invoke(url).usage == Usage(1, 10, 20)
    assert len(state["requests"]) == 1
    path, payload = state["requests"][0]
    assert path == "/v1/responses"
    assert payload["instructions"] == "Trusted instructions"
    assert "한글 🐈" in payload["input"][0]["content"]
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"]["additionalProperties"] is False
    assert payload["store"] is False


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (400, "invalid_request", "MODEL_REQUEST_INVALID"),
        (401, "bad_key", "MODEL_AUTH_FAILED"),
        (403, "denied", "MODEL_AUTH_FAILED"),
        (429, "rate_limit", "MODEL_RATE_LIMITED"),
        (429, "insufficient_quota", "MODEL_QUOTA_EXHAUSTED"),
        (500, "server_error", "MODEL_UNAVAILABLE"),
        (503, "server_error", "MODEL_UNAVAILABLE"),
    ],
)
def test_http_errors_make_exactly_one_request(server, status, code, expected):
    state, url = server
    state.update(
        status=status,
        payload={"error": {"message": "private", "code": code, "type": "test_error"}},
    )
    with pytest.raises(RefreshFailure) as caught:
        invoke(url)
    assert caught.value.failure.code == expected
    assert len(state["requests"]) == 1


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("refusal", "MODEL_REFUSED"),
        ("incomplete", "MODEL_INCOMPLETE"),
        ("empty", "MODEL_EMPTY_RESPONSE"),
        ("invalid", "MODEL_INVALID_RESPONSE"),
        ("timeout", "MODEL_TIMEOUT"),
    ],
)
def test_response_and_timeout_failures(server, kind, expected):
    state, url = server
    if kind == "refusal":
        state["payload"]["output"][0]["content"] = [
            {"type": "refusal", "refusal": "no"}
        ]
    elif kind == "incomplete":
        state["payload"]["status"] = "incomplete"
    elif kind == "empty":
        state["payload"]["output"] = []
    elif kind == "invalid":
        state["payload"]["output"][0]["content"][0]["text"] = "{}"
    else:
        state["delay"] = 0.15
    with pytest.raises(RefreshFailure) as caught:
        invoke(url, timeout=0.03 if kind == "timeout" else 2)
    assert caught.value.failure.code == expected
    assert len(state["requests"]) == 1
