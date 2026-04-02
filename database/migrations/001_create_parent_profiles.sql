-- Migration 001: parent_profiles
-- Run order: first — auth depends on this table

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS parent_profiles (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email               TEXT NOT NULL UNIQUE,
    firebase_uid        TEXT NOT NULL UNIQUE,
    subscription_active BOOLEAN NOT NULL DEFAULT false,
    consent_date        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_parent_profiles_email       ON parent_profiles (email);
CREATE INDEX IF NOT EXISTS idx_parent_profiles_firebase_uid ON parent_profiles (firebase_uid);

-- Auto-update updated_at on every row change
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_parent_profiles_updated_at
    BEFORE UPDATE ON parent_profiles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
