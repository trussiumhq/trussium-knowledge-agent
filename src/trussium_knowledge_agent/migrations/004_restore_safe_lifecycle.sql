ALTER TABLE knowledge_sources
    ADD COLUMN deleted_at timestamptz,
    DROP CONSTRAINT knowledge_sources_lifecycle_state_check,
    ADD CONSTRAINT knowledge_sources_lifecycle_state_check
    CHECK (
        (lifecycle_state = 'active' AND access_lost_at IS NULL
            AND quarantine_expires_at IS NULL AND deleted_at IS NULL)
        OR (lifecycle_state = 'access_lost' AND access_lost_at IS NOT NULL
            AND quarantine_expires_at > access_lost_at
            AND quarantine_expires_at <= access_lost_at + interval '30 days'
            AND deleted_at IS NULL)
        OR (lifecycle_state = 'deleted' AND access_lost_at IS NULL
            AND quarantine_expires_at IS NULL AND deleted_at IS NOT NULL)
    );
