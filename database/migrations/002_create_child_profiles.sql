-- Migration 002: child_profiles
-- Run order: after 001 (parent_profiles)

CREATE TABLE IF NOT EXISTS child_profiles (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id         UUID NOT NULL REFERENCES parent_profiles(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    grade             INT  NOT NULL CHECK (grade BETWEEN 5 AND 8),
    language_code     TEXT NOT NULL DEFAULT 'en',
    current_topic     TEXT,
    weak_topics       TEXT[]       NOT NULL DEFAULT '{}',
    streak_days       INT          NOT NULL DEFAULT 0,
    last_session_date DATE,
    total_sessions    INT          NOT NULL DEFAULT 0,
    total_stars       INT          NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_child_profiles_parent_id ON child_profiles (parent_id);
CREATE INDEX IF NOT EXISTS idx_child_profiles_grade     ON child_profiles (grade);

CREATE TRIGGER trg_child_profiles_updated_at
    BEFORE UPDATE ON child_profiles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
