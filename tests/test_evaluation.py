from __future__ import annotations

import json
from pathlib import Path

import pytest

from trussium_knowledge_agent.evaluation import evaluate_queries, load_dataset
from trussium_knowledge_agent.store import SearchResult


def _result(path: str, anchor: str) -> SearchResult:
    return SearchResult("fixture", None, path, (), anchor, "hash", "text", 0.9)


def test_loads_repository_evaluation_fixture() -> None:
    dataset = load_dataset("fixtures/evaluation-queries.json")

    assert len(dataset) == 6
    assert dataset[0].expected[0].citation == "setup.md#database"


def test_evaluation_reports_hit_recall_and_reciprocal_rank() -> None:
    dataset = load_dataset("fixtures/evaluation-queries.json")[:2]
    report = evaluate_queries(
        dataset,
        lambda query, _limit: (
            [_result("other.md", "unrelated"), _result("setup.md", "database")]
            if "port" in query
            else [_result("wrong.md", "runtime")]
        ),
        limit=2,
    )

    assert report.hit_rate == 0.5
    assert report.mean_recall == 0.5
    assert report.mean_reciprocal_rank == 0.25
    assert report.queries[0].reciprocal_rank == 0.5
    assert report.queries[1].hit == 0.0


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"version": 2, "queries": []}, "version"),
        ({"version": 1, "queries": []}, "between 1 and 100"),
        (
            {
                "version": 1,
                "queries": [
                    {
                        "id": "unsafe",
                        "query": "question",
                        "expected": [{"path": "../secret.md", "anchor": "x"}],
                    }
                ],
            },
            "safe repository-relative",
        ),
    ],
)
def test_dataset_validation_rejects_invalid_data(
    tmp_path: Path, payload: object, message: str
) -> None:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_dataset(path)


def test_evaluation_rejects_out_of_range_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        evaluate_queries(load_dataset("fixtures/evaluation-queries.json"), lambda *_: [], limit=21)
