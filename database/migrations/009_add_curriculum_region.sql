-- Migration 009: add curriculum_region to parent_profiles
-- Run after 008_add_fractions_topic.sql
--
-- Stores the parent's chosen curriculum region. Used to filter arc content
-- so children see culturally relevant hook stories and examples.
--
-- Supported values (matches curriculum YAML region field):
--   canada  — Canadian K-12 curriculum (default)
--   quebec  — Quebec French curriculum (FR-CA context)
--   us      — US Common Core curriculum
--   india   — Indian CBSE curriculum (Phase 2)
--   global  — Region-neutral content (fallback)
--
-- Set during onboarding. Parent can change in Parent Portal.

ALTER TABLE parent_profiles
    ADD COLUMN IF NOT EXISTS curriculum_region VARCHAR(32) NOT NULL DEFAULT 'canada';

-- Backfill any existing rows (already defaulted above, this is explicit)
UPDATE parent_profiles
    SET curriculum_region = 'canada'
    WHERE curriculum_region IS NULL;

CREATE INDEX IF NOT EXISTS idx_parent_profiles_curriculum_region
    ON parent_profiles (curriculum_region);
