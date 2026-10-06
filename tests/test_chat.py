from __future__ import annotations

import json

import httpx
import pytest

from trussium_knowledge_agent.chat import TrussiumChatClient


def test_client_posts_normalized_chat_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer local-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "chat-model"
        assert payload["temperature"] == 0
        assert payload["max_output_tokens"] == 512
        assert payload["stream"] is False
        assert payload["messages"] == [{"role": "user", "content": "Q"}]
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "provider": "self-hosted",
                "model": "resolved-chat-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with TrussiumChatClient(
        "http://runtime.test", api_key="local-secret", http_client=http_client
    ) as client:
        result = client.complete(model="chat-model", messages=[{"role": "user", "content": "Q"}])

    assert result.provider == "self-hosted"
    assert result.model == "resolved-chat-model"
    assert result.content == "{}"
    assert result.finish_reason == "stop"
    http_client.close()


def test_client_sanitizes_runtime_error_response() -> None:
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(503, text="private prompt and credential")
        )
    )
    client = TrussiumChatClient("http://runtime.test", http_client=http_client)

    with pytest.raises(RuntimeError, match="HTTP 503") as error:
        client.complete(model="chat-model", messages=[{"role": "user", "content": "Q"}])

    assert "private prompt" not in str(error.value)
    assert "credential" not in str(error.value)
    http_client.close()


def test_client_rejects_malformed_response() -> None:
    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": []}))
    )
    client = TrussiumChatClient("http://runtime.test", http_client=http_client)

    with pytest.raises(RuntimeError, match="incomplete chat response"):
        client.complete(model="chat-model", messages=[{"role": "user", "content": "Q"}])

    http_client.close()


@pytest.mark.parametrize("timeout", [0, -1, 121, float("inf"), float("nan")])
def test_client_rejects_unbounded_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 120"):
        TrussiumChatClient("http://runtime.test", timeout_seconds=timeout)
