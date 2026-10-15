ALTER TABLE knowledge_sources
    ADD COLUMN github_installation_id bigint,
    ADD COLUMN authorization_verified_until timestamptz,
    ADD CONSTRAINT knowledge_sources_private_github_installation_check
    CHECK (
        github_installation_id IS NULL
        OR (source_kind = 'github' AND github_installation_id > 0)
    ),
    ADD CONSTRAINT knowledge_sources_authorization_lease_check
    CHECK (
        authorization_verified_until IS NULL
        OR github_installation_id IS NOT NULL
    );

CREATE INDEX knowledge_sources_private_installation_idx
    ON knowledge_sources (github_installation_id, source_id)
    WHERE github_installation_id IS NOT NULL;

CREATE TABLE github_webhook_deliveries (
    delivery_id text PRIMARY KEY CHECK (length(delivery_id) BETWEEN 1 AND 100),
    received_at timestamptz NOT NULL DEFAULT now()
);
