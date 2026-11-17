ALTER TABLE knowledge_sources
    ADD COLUMN source_kind text NOT NULL DEFAULT 'local',
    ADD COLUMN source_identity text,
    ADD COLUMN lifecycle_state text NOT NULL DEFAULT 'active',
    ADD COLUMN access_lost_at timestamptz,
    ADD COLUMN quarantine_expires_at timestamptz;

ALTER TABLE knowledge_sources
    ADD CONSTRAINT knowledge_sources_source_kind_check
    CHECK (source_kind IN ('local', 'github')),
    ADD CONSTRAINT knowledge_sources_source_identity_check
    CHECK (
        (source_kind = 'local' AND source_identity IS NULL)
        OR (source_kind = 'github' AND source_identity IS NOT NULL
            AND length(source_identity) BETWEEN 1 AND 256)
    ),
    ADD CONSTRAINT knowledge_sources_lifecycle_state_check
    CHECK (
        (lifecycle_state = 'active' AND access_lost_at IS NULL
            AND quarantine_expires_at IS NULL)
        OR (lifecycle_state = 'access_lost' AND access_lost_at IS NOT NULL
            AND quarantine_expires_at > access_lost_at
            AND quarantine_expires_at <= access_lost_at + interval '30 days')
    );

CREATE INDEX knowledge_sources_quarantine_expiry_idx
    ON knowledge_sources (quarantine_expires_at, source_id)
    WHERE lifecycle_state = 'access_lost';
