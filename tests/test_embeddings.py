from __future__ import annotations

import json

import httpx
import pytest

from trussium_knowledge_agent.embeddings import TrussiumEmbeddingsClient


def test_client_posts_ordered_inputs_and_restores_response_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer local-secret"
        assert json.loads(request.content) == {
            "model": "configured-model",
            "input": ["first", "second"],
        }
        return httpx.Response(
            200,
            json={
                "provider": "self-hosted",
                "model": "resolved-model-v1",
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ],
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with TrussiumEmbeddingsClient(
        "http://runtime.test", api_key="local-secret", http_client=http_client
    ) as client:
        result = client.embed(model="configured-model", inputs=["first", "second"])

    assert result.provider == "self-hosted"
    assert result.model == "resolved-model-v1"
    assert result.vectors == ((1.0, 0.0), (0.0, 1.0))
    assert result.dimension == 2
    assert not http_client.is_closed
    http_client.close()


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 0, "embedding": [1.0, 0.0]}, {"index": 0, "embedding": [0.0, 1.0]}],
        [{"index": 0, "embedding": [1.0, 0.0]}, {"index": 1, "embedding": [0.0]}],
        [{"index": 0, "embedding": [float("nan"), 1.0]}, {"index": 1, "embedding": [0.0, 1.0]}],
        [{"index": 0, "embedding": [0.0, 0.0]}, {"index": 1, "embedding": [0.0, 1.0]}],
    ],
)
def test_client_rejects_invalid_vectors(data: list[dict[str, object]]) -> None:
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=json.dumps(
                    {"provider": "provider", "model": "model", "data": data}
                ).encode(),
            )
        )
    )
    client = TrussiumEmbeddingsClient("http://runtime.test", http_client=http_client)

    with pytest.raises((RuntimeError, TypeError)):
        client.embed(model="model", inputs=["first", "second"])

    http_client.close()


def test_client_does_not_include_runtime_error_body() -> None:
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(503, text="private document text and API key")
        )
    )
    client = TrussiumEmbeddingsClient("http://runtime.test", http_client=http_client)

    with pytest.raises(RuntimeError, match="HTTP 503") as exc_info:
        client.embed(model="model", inputs=["private document"])

    assert "private document" not in str(exc_info.value)
    assert "API key" not in str(exc_info.value)
    http_client.close()


def test_client_rejects_empty_inputs() -> None:
    http_client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    client = TrussiumEmbeddingsClient("http://runtime.test", http_client=http_client)

    with pytest.raises(ValueError, match="non-empty"):
        client.embed(model="model", inputs=[])

    http_client.close()


@pytest.mark.parametrize("timeout", [0, -1, 121, float("inf"), float("nan")])
def test_client_rejects_unbounded_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 120"):
        TrussiumEmbeddingsClient("http://runtime.test", timeout_seconds=timeout)
