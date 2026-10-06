# ADR 0003: Grounded answer contract and citation validation

## Status

Accepted.

## Context

The Knowledge Agent can retrieve a bounded set of Markdown passages by exact
cosine similarity. A useful answer flow must preserve provider neutrality and
show where its evidence came from. Retrieved text is untrusted and may contain
instructions or fabricated source links. A model can also return malformed,
truncated, or uncited output.

## Decision

- Use Trussium's normalized `POST /v1/chat/completions` endpoint through a
  bounded HTTPX client. Keep chat-model configuration separate from the
  embeddings model and do not call provider APIs directly.
- Send at most ten passages, 20,000 characters of passage text, and 32,000 total
  characters of serialized question/evidence to a non-streaming request with a
  4,000-character question limit and a fixed output-token cap.
- Treat the question and evidence as untrusted user data. System instructions
  direct the model to answer only from evidence and ignore embedded instructions.
- Request one JSON object with an `answered` or `insufficient_evidence` status,
  answer text containing `[C#]` markers, and a citation-ID list.
- Require every listed citation ID to exist in the retrieved set and appear in
  the answer, reject unmatched IDs and model-generated URLs, and render source
  metadata only from PostgreSQL records.
- Return a fixed insufficient-evidence message without calling chat when
  retrieval returns no passages. Also fail closed for malformed, truncated,
  uncited, or otherwise unsupported chat output.
- Do not claim these checks prove factual correctness or fully prevent prompt
  injection; retain the source text, question, and runtime privacy boundaries.

## Consequences

The CLI can produce short evidence-backed answers with citations mapped to
stored repository/path/heading/revision metadata. The strict output contract
may reject valid but nonconforming model responses; this is preferable to
presenting unverifiable claims as supported. Generated citations are not
clickable remote links because the index does not store an authoritative source
URL. Evaluation and a browser UI remain later work.

## Alternatives considered

- **Trust model-generated citation URLs:** rejected because URLs are not
  authenticated by retrieval and may be fabricated.
- **Display free-form prose without citation validation:** rejected because
  unsupported claims would be indistinguishable from evidence-backed output.
- **Treat a similarity score threshold as proof of evidence quality:** rejected
  until a representative retrieval evaluation exists.
- **Install the SDK directly from a Git URL:** deferred until a published
  package is available; the narrow runtime HTTP contract avoids an unreleased
  source dependency.

## References

- [Trussium chat API usage](https://github.com/trussiumhq/trussium/blob/main/docs/API_USAGE.md)
- [Trussium Python SDK](https://github.com/trussiumhq/trussium-python)
- [Knowledge Agent architecture](../ARCHITECTURE.md)
