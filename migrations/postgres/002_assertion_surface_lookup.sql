-- Name reuse can make a previously resolved subject reference ambiguous.
CREATE INDEX IF NOT EXISTS idx_assertions_surface_lookup
    ON read_assertions (subject_ref_surface_norm);
