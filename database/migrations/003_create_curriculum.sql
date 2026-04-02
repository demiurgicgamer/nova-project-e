-- Migration 003: curriculum_topics and curriculum_problems
-- Run order: after 002

CREATE TABLE IF NOT EXISTS curriculum_topics (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    grade         INT  NOT NULL CHECK (grade BETWEEN 5 AND 8),
    subject       TEXT NOT NULL DEFAULT 'mathematics',
    topic_key     TEXT NOT NULL,                          -- e.g. 'ratios', 'linear_equations'
    display_name  TEXT NOT NULL,
    order_index   INT  NOT NULL DEFAULT 0,               -- curriculum sequence order
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (grade, topic_key)
);

CREATE INDEX IF NOT EXISTS idx_curriculum_topics_grade ON curriculum_topics (grade);
CREATE INDEX IF NOT EXISTS idx_curriculum_topics_order ON curriculum_topics (grade, order_index);

-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS curriculum_problems (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    topic_id            UUID NOT NULL REFERENCES curriculum_topics(id) ON DELETE CASCADE,
    language_code       TEXT NOT NULL DEFAULT 'en',
    difficulty          INT  NOT NULL CHECK (difficulty BETWEEN 1 AND 5),
    problem_text        TEXT NOT NULL,
    solution_steps      JSONB NOT NULL DEFAULT '[]',      -- array of step strings
    cultural_context    TEXT,                             -- e.g. 'cricket', 'rupees', 'western'
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_curriculum_problems_topic      ON curriculum_problems (topic_id);
CREATE INDEX IF NOT EXISTS idx_curriculum_problems_lang       ON curriculum_problems (language_code);
CREATE INDEX IF NOT EXISTS idx_curriculum_problems_difficulty ON curriculum_problems (difficulty);
CREATE INDEX IF NOT EXISTS idx_curriculum_problems_topic_lang ON curriculum_problems (topic_id, language_code, difficulty);
