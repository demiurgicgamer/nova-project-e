-- Migration 012: topic_stories table
-- Run after 011_create_arc_checkpoint.sql
--
-- Stores the Stage 1 (Hook) narrative stories for each topic.
-- Kept separate from curriculum_problems because:
--   - Stories are never graded (no correct_answer, no distractors)
--   - Multiple stories exist per topic/language for variety
--   - Stories are culture-tagged so the region picker can serve
--     culturally relevant hooks (canada, quebec, us, global)
--   - Future: personalise by child's interest profile (sports, food, etc.)
--
-- One topic can have 2–4 stories per language per culture.
-- CurriculumEngine selects based on:
--   1. parent.curriculum_region   (canada > quebec > us > global)
--   2. child.interest_hint        (sports > food > screen_time > universal)
--   3. Fallback: lowest order_index
--
-- Story text is spoken directly by Nova — keep it conversational, 3–5 sentences.
-- closing_line is Nova's final sentence that bridges to Stage 2.

CREATE TABLE IF NOT EXISTS topic_stories (
    id              UUID    PRIMARY KEY DEFAULT uuid_generate_v4(),
    topic_id        UUID    NOT NULL REFERENCES curriculum_topics(id) ON DELETE CASCADE,
    language_code   TEXT    NOT NULL DEFAULT 'en',

    -- Culture / region tag — matches curriculum_region values + interest hints
    -- Values: 'canadian', 'quebec', 'us', 'global', 'sports', 'food', 'screen_time'
    culture_hint    TEXT    NOT NULL DEFAULT 'global',

    -- The narrative hook — spoken by Nova as Stage 1
    story_text      TEXT    NOT NULL,

    -- Nova's bridging sentence at the end of the story (leads into Stage 2)
    closing_line    TEXT    NOT NULL DEFAULT '',

    -- Order within the same topic/language/culture group (lower = preferred)
    order_index     INT     NOT NULL DEFAULT 0,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Prevent exact duplicate stories for the same topic/language/culture slot
    UNIQUE (topic_id, language_code, culture_hint, order_index)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_topic_stories_topic
    ON topic_stories (topic_id, language_code);

CREATE INDEX IF NOT EXISTS idx_topic_stories_culture
    ON topic_stories (topic_id, language_code, culture_hint);

-- ── Seed: fractions EN hook stories (from reference arc en.md) ───────────────
-- Inserted only if the fractions topic exists in curriculum_topics.

INSERT INTO topic_stories (topic_id, language_code, culture_hint, story_text, closing_line, order_index)
SELECT
    t.id,
    'en',
    'food',
    'You and 3 friends order a pizza. It gets cut into 8 equal slices. You''re hungry — you grab 3 slices. Your friend says: ''that''s not fair, you took more than your share.'' Were they right? How would you even prove it? That''s exactly what fractions let you do.',
    'Fractions are how you settle arguments like this — and I''m going to show you exactly how.',
    0
FROM curriculum_topics t
WHERE t.topic_key = 'fractions' AND t.grade = 6
ON CONFLICT DO NOTHING;

INSERT INTO topic_stories (topic_id, language_code, culture_hint, story_text, closing_line, order_index)
SELECT
    t.id,
    'en',
    'screen_time',
    'You have 60 minutes of screen time today. You spend 20 minutes on YouTube, 15 on a game, and the rest texting. Your parent asks: what fraction of your time went to YouTube? Without fractions, you can''t answer that. With fractions, it takes 5 seconds.',
    'Fractions show up everywhere — time, money, food, sports. Let me show you how they work.',
    1
FROM curriculum_topics t
WHERE t.topic_key = 'fractions' AND t.grade = 6
ON CONFLICT DO NOTHING;

INSERT INTO topic_stories (topic_id, language_code, culture_hint, story_text, closing_line, order_index)
SELECT
    t.id,
    'en',
    'sports',
    'A basketball player takes 8 free throw shots in a game and makes 5 of them. The commentator says she scored ''five eighths'' of her free throws. What does that mean? And is that good or bad? Once you understand fractions, you''ll know instantly.',
    'Every sports stat you''ve ever heard uses fractions. Let''s decode them.',
    2
FROM curriculum_topics t
WHERE t.topic_key = 'fractions' AND t.grade = 6
ON CONFLICT DO NOTHING;

-- ── Seed: fractions FR-CA hook stories ───────────────────────────────────────

INSERT INTO topic_stories (topic_id, language_code, culture_hint, story_text, closing_line, order_index)
SELECT
    t.id,
    'fr',
    'food',
    'Toi et tes 3 amis commandez une poutine géante. Elle est coupée en 8 portions égales. Tu en prends 3. Ton ami dit : « c''est pas juste, t''as pris plus que ta part. » Avait-il raison? Comment est-ce qu''on peut le prouver? C''est exactement ce que les fractions permettent de faire.',
    'Les fractions, c''est comme un arbitre — elles règlent les disputes équitablement. Je vais te montrer comment.',
    0
FROM curriculum_topics t
WHERE t.topic_key = 'fractions' AND t.grade = 6
ON CONFLICT DO NOTHING;

INSERT INTO topic_stories (topic_id, language_code, culture_hint, story_text, closing_line, order_index)
SELECT
    t.id,
    'fr',
    'sports',
    'Un joueur de hockey tire au but 8 fois dans un match et en compte 3. Le commentateur dit qu''il a réussi « trois huitièmes » de ses tirs. C''est quoi exactement trois huitièmes? Et c''est bon ou pas? Avec les fractions, tu peux répondre à ça en 5 secondes.',
    'Les fractions, c''est partout dans le sport, la cuisine, la vie. On commence.',
    1
FROM curriculum_topics t
WHERE t.topic_key = 'fractions' AND t.grade = 6
ON CONFLICT DO NOTHING;
