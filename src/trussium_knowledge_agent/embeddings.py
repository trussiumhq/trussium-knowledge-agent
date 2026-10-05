"""Validated client for Trussium's provider-neutral embeddings API."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, Self

import httpx

MAX_VECTOR_DIMENSIONS = 16_000


@dataclass(frozen=True, slots=True)
class EmbeddingsBatch:
    """Vectors returned for one ordered runtime request."""

    provider: str
    model: str
    vectors: tuple[tuple[float, ...], ...]

    @property
    def dimension(self) -> int:
        return len(self.vectors[0])


class EmbeddingsClient(Protocol):
    """Minimal interface used by indexing and query embedding."""

    def embed(self, *, model: str, inputs: Sequence[str]) -> EmbeddingsBatch:
        """Create vectors for input text in request order."""


class TrussiumEmbeddingsClient:
    """Call the Trussium runtime without exposing provider-specific APIs."""

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
        self._url = f"{base_url.rstrip('/')}/v1/embeddings"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
        )
        self._owns_client = http_client is None

    def close(self) -> None:
        """Close the underlying HTTP client when this instance created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def embed(self, *, model: str, inputs: Sequence[str]) -> EmbeddingsBatch:
        """Return validated vectors in input order, with sanitized failures."""
        if not model.strip() or not inputs or any(not text.strip() for text in inputs):
            raise ValueError("model and embedding inputs must be non-empty")
        try:
            response = self._client.post(
                self._url,
                headers=self._headers,
                json={"model": model, "input": list(inputs)},
            )
        except httpx.TimeoutException as error:
            raise RuntimeError("Trussium embeddings request timed out.") from error
        except httpx.HTTPError as error:
            raise RuntimeError("Trussium embeddings request failed.") from error
        if response.is_error:
            raise RuntimeError(
                f"Trussium embeddings request failed with HTTP {response.status_code}."
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError("Trussium returned an invalid embeddings response.") from error
        return _validate_batch(payload, expected_count=len(inputs))


def _validate_batch(payload: object, *, expected_count: int) -> EmbeddingsBatch:
    if not isinstance(payload, Mapping):
        raise TypeError("Trussium returned an invalid embeddings response.")
    provider = payload.get("provider")
    model = payload.get("model")
    data = payload.get("data")
    if (
        not isinstance(provider, str)
        or not provider.strip()
        or not isinstance(model, str)
        or not model.strip()
        or not isinstance(data, list)
        or len(data) != expected_count
    ):
        raise RuntimeError("Trussium returned an incomplete embeddings response.")

    vectors: list[tuple[float, ...] | None] = [None] * expected_count
    dimension: int | None = None
    for item in data:
        if not isinstance(item, Mapping):
            raise TypeError("Trussium returned malformed embedding data.")
        index = item.get("index")
        values = item.get("embedding")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < expected_count:
            raise RuntimeError("Trussium returned invalid embedding indexes.")
        if vectors[index] is not None or not isinstance(values, list):
            raise RuntimeError("Trussium returned duplicate or malformed embedding data.")
        if not values or len(values) > MAX_VECTOR_DIMENSIONS:
            raise RuntimeError("Trussium returned an unsupported vector dimension.")
        parsed: list[float] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError("Trussium returned a non-numeric vector value.")
            component = float(value)
            if not math.isfinite(component):
                raise RuntimeError("Trussium returned a non-finite vector value.")
            parsed.append(component)
        if dimension is None:
            dimension = len(parsed)
        if len(parsed) != dimension:
            raise RuntimeError("Trussium returned vectors with inconsistent dimensions.")
        if not any(component != 0.0 for component in parsed):
            raise RuntimeError("Trussium returned a zero vector; cosine search is undefined.")
        vectors[index] = tuple(parsed)

    if any(vector is None for vector in vectors):
        raise RuntimeError("Trussium returned incomplete embedding indexes.")
    return EmbeddingsBatch(
        provider=provider,
        model=model,
        vectors=tuple(vector for vector in vectors if vector is not None),
    )
