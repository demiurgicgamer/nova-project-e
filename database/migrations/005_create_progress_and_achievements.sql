-- Migration 005: child_topic_progress and achievements
-- Run order: after 002, 003, 004

CREATE TABLE IF NOT EXISTS child_topic_progress (
    child_id        UUID NOT NULL REFERENCES child_profiles(id) ON DELETE CASCADE,
    topic_id        UUID NOT NULL REFERENCES curriculum_topics(id) ON DELETE CASCADE,
    mastery_level   INT  NOT NULL DEFAULT 0 CHECK (mastery_level BETWEEN 0 AND 100),
    status          TEXT NOT NULL DEFAULT 'not_started',  -- 'not_started','needs_review','progressing','mastered'
    correct_count   INT  NOT NULL DEFAULT 0,
    attempt_count   INT  NOT NULL DEFAULT 0,
    last_attempted  TIMESTAMPTZ,
    PRIMARY KEY (child_id, topic_id)
);

CREATE INDEX IF NOT EXISTS idx_topic_progress_child  ON child_topic_progress (child_id);
CREATE INDEX IF NOT EXISTS idx_topic_progress_status ON child_topic_progress (child_id, status);

-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS achievements (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    child_id     UUID NOT NULL REFERENCES child_profiles(id) ON DELETE CASCADE,
    badge_key    TEXT NOT NULL,                           -- 'first_session','streak_7','topic_mastered', etc.
    earned_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id   UUID REFERENCES sessions(id) ON DELETE SET NULL,
    UNIQUE (child_id, badge_key)                         -- each badge earned once
);

CREATE INDEX IF NOT EXISTS idx_achievements_child ON achievements (child_id);
