CREATE TABLE knowledge_sources (
    source_id text PRIMARY KEY CHECK (source_id ~ '^[0-9a-f]{64}$'),
    display_name text NOT NULL,
    revision text,
    indexed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE document_chunks (
    source_id text NOT NULL REFERENCES knowledge_sources(source_id) ON DELETE CASCADE,
    chunk_id text NOT NULL CHECK (chunk_id ~ '^[0-9a-f]{64}$'),
    relative_path text NOT NULL,
    heading_path text[] NOT NULL DEFAULT '{}',
    heading_anchor text NOT NULL,
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    content text NOT NULL CHECK (length(content) > 0),
    PRIMARY KEY (source_id, chunk_id)
);

CREATE INDEX document_chunks_source_path_idx
    ON document_chunks (source_id, relative_path);
