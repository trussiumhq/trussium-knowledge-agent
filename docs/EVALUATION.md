# Retrieval evaluation

The checked-in evaluation corpus provides a repeatable smoke test for retrieval
and source-citation coverage. It measures whether expected Markdown passages
appear in the ranked results; it does not use an LLM judge and does not prove
that generated answers are factually correct.

## Run the fixture

Start PostgreSQL and a Trussium runtime with an embeddings-enabled model, then
set `DATABASE_URL`, `TRUSSIUM_URL`, and `TRUSSIUM_EMBEDDING_MODEL` as described
in the [local setup](../README.md#local-development). Index the small fixture
corpus and run its evaluation queries:

```bash
uv run --env-file .env trussium-knowledge-agent index fixtures/evaluation-corpus
uv run --env-file .env trussium-knowledge-agent evaluate fixtures/evaluation-queries.json --limit 5
```

Evaluation does not index or mutate sources by itself. The corpus must first be
indexed with the same embedding provider and model used for evaluation. The
index command replaces the fixture source atomically when rerun. Remove it
afterward if desired:

```bash
uv run --env-file .env trussium-knowledge-agent remove fixtures/evaluation-corpus
```

## Dataset format

Datasets are UTF-8 JSON objects with `version: 1` and a `queries` array. Each
query has a unique `id`, non-empty question text, and one or more expected
passages identified by their corpus-relative POSIX `path` and Markdown heading
`anchor`:

```json
{
  "version": 1,
  "queries": [
    {
      "id": "postgres-port",
      "query": "Which port does the local PostgreSQL database use?",
      "expected": [{"path": "setup.md", "anchor": "database"}]
    }
  ]
}
```

The loader limits a dataset to 1 MiB, 100 queries, 4,000 characters per query,
and 10 expected passages per query. Paths must be relative and cannot traverse
outside the corpus. Dataset validation fails closed on unknown fields, invalid
versions, duplicate IDs, or malformed expectations.

## Metrics and interpretation

- **Hit@k**: fraction of queries with at least one expected passage in the
  first `k` retrieved results.
- **Recall@k**: fraction of each query's expected passages found in its first
  `k` results, averaged across queries.
- **MRR@k**: mean reciprocal rank of the first expected passage within the
  first `k` results; a miss contributes zero.

Expected passages are matched by `relative_path#heading_anchor`, the same
stable location shown in search and answer citations. Results can vary with the
configured embedding model, corpus edits, and stored index. Keep fixture content
small and deterministic, and review metric changes rather than treating one
score as a universal quality threshold. These metrics assess retrieval and
citation coverage only—not answer faithfulness, completeness, or usefulness.

## Recorded local reference run

On 2026-10-07, the six-query fixture was indexed as 3 Markdown files and 6
chunks using PostgreSQL 16.15 with pgvector 0.8.7 and the local
OpenAI-compatible Ollama model `nomic-embed-text:latest` (768 dimensions).
The run returned Hit@5 1.000, Recall@5 1.000, and MRR@5 1.000. This is one
environment-specific reference result, not a guaranteed threshold for other
models or corpus versions.
