-- Migration 011: arc_checkpoint table
-- Run after 010_add_arc_stage.sql
--
-- Tracks exactly where each child is inside each topic arc so Nova can
-- resume precisely where the child left off, respecting the arc rules:
--
--   First visit  → stages delivered in order: hook → concept → guided → practice → capstone
--   Return visit → resume from checkpoint (never go backwards)
--   3-day gap    → insert one warm-up problem from the previous stage
--                  before continuing (see current_stage + last_seen logic)
--   Post-mastery → stage = 'review'; Nova surfaces S4–S5 problems only
--
-- One row per (child, topic).  Upserted by CurriculumEngine on every
-- problem attempt and on session_pause / session_end events.

CREATE TABLE IF NOT EXISTS arc_checkpoint (
    child_id            UUID    NOT NULL REFERENCES child_profiles(id)      ON DELETE CASCADE,
    topic_id            UUID    NOT NULL REFERENCES curriculum_topics(id)   ON DELETE CASCADE,

    -- Current position in the arc
    current_stage       TEXT    NOT NULL DEFAULT 'hook'
                            CHECK (current_stage IN ('hook','concept','guided','practice','capstone','review')),
    current_problem_id  UUID    REFERENCES curriculum_problems(id)          ON DELETE SET NULL,

    -- Which stages have been fully passed (array of stage names)
    -- e.g. ARRAY['hook','concept','guided']
    completed_stages    TEXT[]  NOT NULL DEFAULT '{}',

    -- Per-stage attempt counts and correct counts — stored as JSONB
    -- e.g. {"guided": {"attempts": 2, "correct": 1}, "practice": {"attempts": 5, "correct": 4}}
    stage_stats         JSONB   NOT NULL DEFAULT '{}',

    -- Timestamps for resume + warm-up gap detection
    first_seen          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Denormalised for quick mastery reads (mirrors child_topic_progress.mastery_level)
    -- Updated in sync with child_topic_progress so CurriculumEngine has one join less.
    mastery_snapshot    INT     NOT NULL DEFAULT 0 CHECK (mastery_snapshot BETWEEN 0 AND 100),

    PRIMARY KEY (child_id, topic_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_arc_checkpoint_child
    ON arc_checkpoint (child_id);

CREATE INDEX IF NOT EXISTS idx_arc_checkpoint_child_stage
    ON arc_checkpoint (child_id, current_stage);

-- Partial index for topics currently in-progress (not yet mastered)
CREATE INDEX IF NOT EXISTS idx_arc_checkpoint_in_progress
    ON arc_checkpoint (child_id, topic_id)
    WHERE current_stage NOT IN ('review');

-- ── Helper comment for CurriculumEngine ──────────────────────────────────────
-- 3-day warm-up rule:
--   IF last_seen < NOW() - INTERVAL '3 days'
--   AND current_stage NOT IN ('hook', 'review')
--   THEN serve one problem from the previous stage before advancing.
--
-- Stage advance logic (managed by CurriculumEngine, not enforced by DB):
--   hook      → passes automatically when story is delivered
--   concept   → passes when comprehension_check answered correctly (retries allowed)
--   guided    → passes when guided problem solved correctly (retries allowed)
--   practice  → passes when 2 out of 3 (or 3 out of 5) problems correct
--   capstone  → passes when both parts correct within 2 attempts
--   review    → permanent; Nova surfaces S4–S5 problems as warm-ups
