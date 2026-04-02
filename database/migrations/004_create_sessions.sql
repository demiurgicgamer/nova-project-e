-- Migration 004: sessions and session_events
-- Run order: after 002 (child_profiles)

CREATE TABLE IF NOT EXISTS sessions (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    child_id         UUID NOT NULL REFERENCES child_profiles(id) ON DELETE CASCADE,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at         TIMESTAMPTZ,
    duration_seconds INT,
    topics_covered   TEXT[]  NOT NULL DEFAULT '{}',
    correct_answers  INT     NOT NULL DEFAULT 0,
    total_questions  INT     NOT NULL DEFAULT 0,
    stars_earned     INT     NOT NULL DEFAULT 0 CHECK (stars_earned BETWEEN 0 AND 3),
    emotion_summary  JSONB   NOT NULL DEFAULT '{}',       -- {dominant, counts: {CONFUSED:2, ...}}
    language_code    TEXT    NOT NULL DEFAULT 'en',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sessions_child_id   ON sessions (child_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_child_date ON sessions (child_id, started_at DESC);

-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS session_events (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id   UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    event_type   TEXT NOT NULL,                           -- 'question_asked', 'answer_given', 'hint_given', 'emotion_detected'
    event_data   JSONB NOT NULL DEFAULT '{}',
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events (session_id);
CREATE INDEX IF NOT EXISTS idx_session_events_type    ON session_events (event_type);
