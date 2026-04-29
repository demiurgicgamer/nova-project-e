-- Migration 013: retag pre-arc seed problems with correct arc stage
-- Run after 012_create_topic_stories.sql
--
-- Context
-- -------
-- Migration 010 added the `stage` column with DEFAULT 'practice' and partially
-- tagged fractions (difficulty ≤ 2 → 'guided').  However, all other seed topics
-- (ratios, percentages, linear_equations, and any topic seeded via seed.js /
-- earlier migrations) still sit at the default 'practice' stage regardless of
-- their difficulty.
--
-- Arc problems inserted by insert_arc.py are distinguishable because they have
-- cultural_context set to the JSON problem ID (e.g. 'p_guided_1', 'p_practice_2').
-- Pre-arc seed problems have cultural_context = NULL or '' — that is the guard
-- used below to avoid touching arc content.
--
-- Retag rules (mirrors ARC_FRAMEWORK.md difficulty bands):
--   difficulty 1     → 'guided'    (heavily scaffolded first attempts)
--   difficulty 2     → 'guided'    (still guided, slightly harder)
--   difficulty 3     → 'practice'  (independent — already correct default)
--   difficulty 4     → 'capstone'  (synthesis; unlikely in seed data but handle it)
--   difficulty 5     → 'capstone'
--
-- The UPDATE is idempotent — safe to re-run.

-- ── 1. Seed problems: difficulty 1 → guided ───────────────────────────────────
UPDATE curriculum_problems
SET stage = 'guided'
WHERE difficulty = 1
  AND stage = 'practice'
  AND (cultural_context IS NULL OR cultural_context = '');

-- ── 2. Seed problems: difficulty 2 → guided ───────────────────────────────────
UPDATE curriculum_problems
SET stage = 'guided'
WHERE difficulty = 2
  AND stage = 'practice'
  AND (cultural_context IS NULL OR cultural_context = '');

-- ── 3. Seed problems: difficulty 4–5 → capstone ──────────────────────────────
UPDATE curriculum_problems
SET stage = 'capstone'
WHERE difficulty >= 4
  AND stage = 'practice'
  AND (cultural_context IS NULL OR cultural_context = '');

-- ── 4. Verify — uncomment to audit after running ──────────────────────────────
-- SELECT
--     ct.topic_key,
--     cp.language_code,
--     cp.difficulty,
--     cp.stage,
--     COUNT(*) AS count
-- FROM curriculum_problems cp
-- JOIN curriculum_topics ct ON ct.id = cp.topic_id
-- WHERE cp.cultural_context IS NULL OR cp.cultural_context = ''
-- GROUP BY ct.topic_key, cp.language_code, cp.difficulty, cp.stage
-- ORDER BY ct.topic_key, cp.language_code, cp.difficulty;
