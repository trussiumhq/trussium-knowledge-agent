"""Validated client for Trussium's provider-neutral chat API."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, Self

import httpx


@dataclass(frozen=True, slots=True)
class ChatCompletion:
    """Small normalized completion consumed by answer validation."""

    provider: str
    model: str
    content: str
    finish_reason: str


class ChatClient(Protocol):
    """Minimal interface used by grounded answer generation."""

    def complete(self, *, model: str, messages: list[dict[str, str]]) -> ChatCompletion:
        """Create one non-streaming chat completion."""


class TrussiumChatClient:
    """Call Trussium's normalized chat endpoint without provider-specific APIs."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 0 and 120 seconds")
        self._url = f"{base_url.rstrip('/')}/v1/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._client = http_client or httpx.Client(timeout=httpx.Timeout(timeout_seconds))
        self._owns_client = http_client is None

    def close(self) -> None:
        """Close the underlying HTTP client when this instance created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def complete(self, *, model: str, messages: list[dict[str, str]]) -> ChatCompletion:
        """Request a bounded non-streaming completion and validate its envelope."""
        try:
            response = self._client.post(
                self._url,
                headers=self._headers,
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0,
                    "max_output_tokens": 512,
                    "stream": False,
                },
            )
        except httpx.TimeoutException as error:
            raise RuntimeError("Trussium chat request timed out.") from error
        except httpx.HTTPError as error:
            raise RuntimeError("Trussium chat request failed.") from error
        if response.is_error:
            raise RuntimeError(f"Trussium chat request failed with HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError("Trussium returned an invalid chat response.") from error
        return _validate_completion(payload)


def _validate_completion(payload: object) -> ChatCompletion:
    if not isinstance(payload, Mapping):
        raise TypeError("Trussium returned an invalid chat response.")
    provider = payload.get("provider")
    model = payload.get("model")
    choices = payload.get("choices")
    if (
        not isinstance(provider, str)
        or not provider.strip()
        or not isinstance(model, str)
        or not model.strip()
        or not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], Mapping)
    ):
        raise RuntimeError("Trussium returned an incomplete chat response.")
    message = choices[0].get("message")
    finish_reason = choices[0].get("finish_reason")
    if (
        not isinstance(message, Mapping)
        or message.get("role") != "assistant"
        or not isinstance(message.get("content"), str)
        or not isinstance(finish_reason, str)
    ):
        raise RuntimeError("Trussium returned malformed chat completion data.")
    return ChatCompletion(provider, model, message["content"], finish_reason)
