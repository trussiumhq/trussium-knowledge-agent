# ADR 0002: Embedding identity and exact retrieval

## Status

Accepted.

## Context

The app stores chunks from local Markdown repositories and delegates embeddings
to the configured Trussium runtime. The runtime returns a resolved provider
and model with each vector batch; model aliases can resolve to concrete model
names, and different providers/models can return different dimensions.
Comparing vectors across those identities is invalid. The first reference
corpus is small, and its embedding dimensions are operator-selected.

## Decision

- Call Trussium's provider-neutral `POST /v1/embeddings` API through a bounded
  HTTP client. Do not call provider-specific endpoints or add a Git-sourced
  Python SDK dependency; the SDK's current synchronous method is the same HTTP
  operation, while a PyPI release is not yet available.
- Embed in batches of at most 64 inputs, preserve the response index mapping,
  and validate non-empty finite non-zero vectors with a consistent dimension.
- Store vectors using pgvector's variable-dimension `vector` type. Persist the
  provider, resolved model, and dimension with every chunk.
- Only compare query and document vectors with the same provider, resolved
  model, and dimension. Report `1 - cosine_distance` as the similarity score.
- Start with exact cosine search and a bounded result limit. Defer approximate
  indexes until measured corpus size and a stable model dimension justify an
  index strategy.
- Provision the pgvector extension during database setup; schema migrations
  must not require the application database role to create extensions.
- Complete embedding requests before replacing an existing source index. A
  runtime failure therefore leaves the previous indexed source available.

## Consequences

The application can search across sources that use the same embedding identity
without mixing incompatible vector spaces. Model changes require re-indexing
before the new identity can retrieve old source chunks. Exact search is simple
and deterministic for a demo corpus but scans matching vectors and may need an
approximate index after evaluation at larger scale. The operator must configure
an embeddings-capable Trussium model and understand that selected chunk and
query text is sent to that runtime.

## Alternatives considered

- **Direct provider API calls:** rejected because provider credentials and
  provider-specific contracts belong to Trussium.
- **Automatically mix model identities:** rejected because dimensions and
  vector spaces are not generally comparable.
- **Add HNSW/IVFFlat immediately:** deferred because the app accepts different
  model dimensions and has no retrieval evaluation or corpus-size evidence yet.
- **Install the Python SDK from a Git URL:** rejected until the SDK is
  published as an installable package; a Git dependency complicates package
  auditing and release reproducibility.

## References

- [Trussium embeddings contract](https://github.com/trussiumhq/trussium/blob/main/docs/EMBEDDINGS.md)
- [Trussium Python SDK](https://github.com/trussiumhq/trussium-python)
- [pgvector dimension and cosine-search behavior](https://github.com/pgvector/pgvector)
