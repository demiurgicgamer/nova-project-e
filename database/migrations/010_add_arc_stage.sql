-- Migration 010: add arc stage to curriculum_problems
-- Run after 009_add_curriculum_region.sql
--
-- Adds a `stage` column so every problem is tagged to its position in the
-- 5-stage arc (hook → concept → guided → practice → capstone).
--
-- Stage meanings:
--   hook       — narrative story, no grading (S1)
--   concept    — explanation + comprehension check (S2)
--   guided     — first attempt with full scaffolding (S3)
--   practice   — independent problems, 2/3 pass required (S4)
--   capstone   — multi-step synthesis, no hints until attempt 2 (S5)
--
-- Default: 'practice' — preserves behaviour for all pre-arc seed problems
-- (migrations 007 + 008) which were practice-level problems.
--
-- Arc delivery order enforced by CurriculumEngine, not by DB constraint.

ALTER TABLE curriculum_problems
    ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'practice'
        CHECK (stage IN ('hook', 'concept', 'guided', 'practice', 'capstone'));

-- Index: CurriculumEngine queries by topic + language + stage + difficulty
CREATE INDEX IF NOT EXISTS idx_curriculum_problems_stage
    ON curriculum_problems (topic_id, language_code, stage);

CREATE INDEX IF NOT EXISTS idx_curriculum_problems_stage_diff
    ON curriculum_problems (topic_id, language_code, stage, difficulty);

-- ── Backfill existing seed problems ──────────────────────────────────────────
-- All pre-arc seed data (007 + 008) is practice-level — already defaulted.
-- Explicitly tag fractions problems (008) which have a known mix:
--   difficulty 1-2 → guided, difficulty 3 → practice
-- This is best-effort; proper stage tagging happens via insert_arc.py.

UPDATE curriculum_problems
SET stage = 'guided'
WHERE topic_id IN (SELECT id FROM curriculum_topics WHERE topic_key = 'fractions')
  AND difficulty <= 2
  AND stage = 'practice';   -- only touch default-value rows
