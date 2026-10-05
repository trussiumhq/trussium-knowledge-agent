from __future__ import annotations

import math

import pytest

from trussium_knowledge_agent.store import search_chunks


@pytest.mark.parametrize("top_k", [0, 21])
def test_search_rejects_unbounded_result_limits(top_k: int) -> None:
    with pytest.raises(ValueError, match="top_k"):
        search_chunks(
            "not-used",
            query_vector=(1.0, 0.0),
            provider="provider",
            model="model",
            top_k=top_k,
        )


def test_search_rejects_nonfinite_query_vector() -> None:
    with pytest.raises(ValueError):
        search_chunks(
            "not-used",
            query_vector=(math.nan,),
            provider="provider",
            model="model",
        )
