ALTER TABLE document_chunks
    ADD COLUMN embedding vector,
    ADD COLUMN embedding_provider text,
    ADD COLUMN embedding_model text,
    ADD COLUMN embedding_dimension integer;

ALTER TABLE document_chunks
    ADD CONSTRAINT document_chunks_embedding_identity_check
    CHECK (
        (embedding IS NULL AND embedding_provider IS NULL AND embedding_model IS NULL
            AND embedding_dimension IS NULL)
        OR
        (embedding IS NOT NULL AND embedding_provider IS NOT NULL
            AND length(embedding_provider) > 0 AND embedding_model IS NOT NULL
            AND length(embedding_model) > 0 AND embedding_dimension BETWEEN 1 AND 16000
            AND vector_dims(embedding) = embedding_dimension)
    );

CREATE INDEX document_chunks_embedding_identity_idx
    ON document_chunks (embedding_provider, embedding_model, embedding_dimension)
    WHERE embedding IS NOT NULL;
