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

## Guidance-review cases

The separate `fixtures/guidance-review-cases.json` dataset contains six
synthetic cases with proposed expected labels for human review: direct conflicts,
compatible details, and insufficient evidence. Each record includes a rationale
and the only evidence the reviewer may cite. Review and revise these labels as
needed for the intended domain. Run the inspection with a configured Trussium chat
model:

```bash
uv run --env-file .env trussium-knowledge-agent evaluate-guidance fixtures/guidance-review-cases.json
```

This command does not require PostgreSQL or an embedding model because its
evidence is fixture-supplied rather than retrieved. The case text is sent to the
configured Trussium chat endpoint, so use a runtime whose data handling is
appropriate for the fixture and do not replace synthetic text with sensitive
content. Output shows expected and observed labels, cited locations, rationale,
and the model assessment case by case. It intentionally does not calculate an
aggregate accuracy score or establish a pass threshold. Status and citation
agreement are prompts for human inspection, not calibrated quality measures;
review divergences before changing prompts or confidence language. These small
fixtures are illustrative and are not claimed to represent production corpora.

## Generated answer-quality review

The separate `fixtures/answer-quality-cases.json` dataset contains four fixed,
synthetic question/evidence cases. They cover a multi-fact answer, selecting the
supporting passage among a distractor, abstaining when evidence is irrelevant,
and treating instruction-like text inside evidence as untrusted. Each case has
human-authored required points, expected citations, a reference answer, and a
rationale to guide inspection.

Run the review with a reachable Trussium chat runtime and a configured chat
model:

```bash
uv run --env-file .env trussium-knowledge-agent evaluate-answers fixtures/answer-quality-cases.json
```

This command uses only the fixed fixture passages; it does not read the index,
connect to PostgreSQL, or call an embedding model. It makes one chat request for
each case, so it requires a reachable runtime and may incur provider usage. The
default runtime URL is `http://127.0.0.1:9000`; `TRUSSIUM_URL`,
`TRUSSIUM_CHAT_MODEL`, and optional `TRUSSIUM_API_KEY` follow the same
configuration as `ask`. Do not point it at an unapproved remote service.

Only the synthetic case question and evidence should be sent. Do not replace
the fixture with private or production documents. The evidence intentionally
includes an instruction-like string in one case; this tests the existing
untrusted-evidence boundary and is not permission to put secrets in prompts.

For each case, compare the observed response with the reference answer and
required points, then record reviewer notes alongside the runtime/model and
date. Check that each factual claim is supported by the citation beside it,
required facts are covered without unsupported additions, citation locations
are accurate, and answering versus abstaining is appropriate. A reference
answer is a review aid, not the sole valid wording.

The command deliberately calculates no answer-quality score, agreement rate,
or pass threshold. Its output varies with the chat model and runtime; use the
same case version and record the model when comparing manual runs. Structural
tests check dataset bounds and citation references, not semantic correctness.
This review does not establish calibrated accuracy, answer truth, or production
readiness. Retrieval metrics above remain separate and measure only retrieval
and citation coverage.

## Recorded local reference run

On 2026-10-07, the six-query fixture was indexed as 3 Markdown files and 6
chunks using PostgreSQL 16.15 with pgvector 0.8.7 and the local
OpenAI-compatible Ollama model `nomic-embed-text:latest` (768 dimensions).
The run returned Hit@5 1.000, Recall@5 1.000, and MRR@5 1.000. This is one
environment-specific reference result, not a guaranteed threshold for other
models or corpus versions.
