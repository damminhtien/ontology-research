CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS identity_entities (
    entity_id text PRIMARY KEY,
    entity_type text NOT NULL
);

CREATE TABLE IF NOT EXISTS identity_aliases (
    entity_id text NOT NULL REFERENCES identity_entities(entity_id),
    normalized_alias text NOT NULL,
    surface text NOT NULL,
    PRIMARY KEY (entity_id, normalized_alias, surface)
);
CREATE INDEX IF NOT EXISTS idx_identity_alias_lookup
    ON identity_aliases (normalized_alias, entity_id);
CREATE INDEX IF NOT EXISTS idx_identity_alias_trigram
    ON identity_aliases USING gin (normalized_alias gin_trgm_ops);

CREATE TABLE IF NOT EXISTS identity_alias_tokens (
    token text NOT NULL,
    normalized_alias text NOT NULL,
    PRIMARY KEY (token, normalized_alias)
);
CREATE INDEX IF NOT EXISTS idx_identity_alias_tokens_alias
    ON identity_alias_tokens (normalized_alias);

CREATE TABLE IF NOT EXISTS identity_external_ids (
    entity_id text NOT NULL REFERENCES identity_entities(entity_id),
    namespace text NOT NULL,
    external_id text NOT NULL,
    PRIMARY KEY (entity_id, namespace, external_id)
);
CREATE INDEX IF NOT EXISTS idx_external_ids_lookup
    ON identity_external_ids (namespace, external_id);

CREATE TABLE IF NOT EXISTS identity_source_records (
    source_id text NOT NULL,
    record_id text NOT NULL,
    entity_id text NOT NULL REFERENCES identity_entities(entity_id),
    PRIMARY KEY (source_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_source_record_entity
    ON identity_source_records (entity_id);

CREATE TABLE IF NOT EXISTS identity_merges (
    duplicate_id text PRIMARY KEY REFERENCES identity_entities(entity_id),
    survivor_id text NOT NULL REFERENCES identity_entities(entity_id),
    CHECK (duplicate_id <> survivor_id)
);
CREATE INDEX IF NOT EXISTS idx_identity_merge_survivor
    ON identity_merges (survivor_id);

CREATE TABLE IF NOT EXISTS identity_cannot_links (
    left_id text NOT NULL REFERENCES identity_entities(entity_id),
    right_id text NOT NULL REFERENCES identity_entities(entity_id),
    evidence jsonb NOT NULL,
    PRIMARY KEY (left_id, right_id),
    CHECK (left_id < right_id)
);

CREATE TABLE IF NOT EXISTS read_entities (
    entity_id text PRIMARY KEY,
    entity_type text NOT NULL,
    name text NOT NULL,
    name_key text NOT NULL,
    first_seen text NOT NULL,
    last_event_time text NOT NULL,
    merged_into text
);
CREATE INDEX IF NOT EXISTS idx_read_entity_name
    ON read_entities (name_key) WHERE merged_into IS NULL;
CREATE INDEX IF NOT EXISTS idx_read_entity_name_trigram
    ON read_entities USING gin (name_key gin_trgm_ops) WHERE merged_into IS NULL;
CREATE INDEX IF NOT EXISTS idx_read_entity_merge
    ON read_entities (merged_into) WHERE merged_into IS NOT NULL;

CREATE TABLE IF NOT EXISTS read_entity_names (
    entity_id text NOT NULL REFERENCES read_entities(entity_id),
    normalized_name text NOT NULL,
    surface text NOT NULL,
    PRIMARY KEY (entity_id, normalized_name, surface)
);
CREATE INDEX IF NOT EXISTS idx_read_entity_names_lookup
    ON read_entity_names (normalized_name, entity_id);

CREATE TABLE IF NOT EXISTS read_assertions (
    assertion_id text PRIMARY KEY,
    subject_id text NOT NULL DEFAULT '',
    original_subject_id text NOT NULL DEFAULT '',
    subject_ref jsonb,
    subject_ref_surface_norm text,
    subject_status text NOT NULL,
    predicate_iri text NOT NULL,
    object jsonb NOT NULL,
    object_entity_id text,
    valid_from timestamptz NOT NULL,
    valid_from_text text NOT NULL,
    valid_until timestamptz,
    valid_until_text text,
    recorded_at text NOT NULL DEFAULT '',
    source_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    model_score double precision,
    evidence_quality text NOT NULL DEFAULT 'unknown',
    status text NOT NULL,
    superseded_by text,
    superseded_reason text
);
CREATE INDEX IF NOT EXISTS idx_assertions_subject_predicate_time
    ON read_assertions (subject_id, predicate_iri, valid_from);
CREATE INDEX IF NOT EXISTS idx_assertions_object
    ON read_assertions (object_entity_id) WHERE object_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_assertions_location_interval
    ON read_assertions (subject_id, valid_from, valid_until)
    WHERE predicate_iri = 'https://w3id.org/semantic-foundry/core#locatedAt'
      AND status = 'asserted';
CREATE INDEX IF NOT EXISTS idx_assertions_pending_surface
    ON read_assertions (subject_ref_surface_norm)
    WHERE subject_ref_surface_norm IS NOT NULL AND subject_id = '';

CREATE TABLE IF NOT EXISTS read_tracks (
    track_id text PRIMARY KEY,
    entity_id text NOT NULL,
    subject_name text NOT NULL DEFAULT '',
    observation_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_ids jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_read_tracks_entity ON read_tracks (entity_id);

CREATE TABLE IF NOT EXISTS projection_events (
    event_id text PRIMARY KEY,
    sequence bigint UNIQUE,
    applied boolean NOT NULL
);
CREATE TABLE IF NOT EXISTS projection_state (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    checkpoint_sequence bigint,
    applied_count bigint NOT NULL DEFAULT 0,
    last_event_time timestamptz
);
INSERT INTO projection_state(singleton, checkpoint_sequence, applied_count)
VALUES (TRUE, NULL, 0)
ON CONFLICT (singleton) DO NOTHING;
