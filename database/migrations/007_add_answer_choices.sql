-- 007_add_answer_choices.sql
-- Adds structured answer data to curriculum_problems so Unity can display
-- tap-to-answer multiple-choice questions on the question card.
--
-- correct_answer:     the final answer string (e.g. "3/5", "$2.00")
-- distractor_answers: array of 3 plausible wrong answers
--
-- If empty, the question card falls back to voice-only mode (no tap choices).

ALTER TABLE curriculum_problems
    ADD COLUMN IF NOT EXISTS correct_answer     TEXT    DEFAULT '',
    ADD COLUMN IF NOT EXISTS distractor_answers TEXT[]  DEFAULT '{}';

-- Backfill seed data for the Grade 6 math problems
-- (only if the problems table already has these topic rows)

UPDATE curriculum_problems
SET correct_answer = '3/5', distractor_answers = ARRAY['2/5', '12/8', '4/5']
WHERE topic_id IN (SELECT id FROM curriculum_topics WHERE topic_key = 'ratios')
  AND language_code = 'en'
  AND correct_answer = '';

UPDATE curriculum_problems
SET correct_answer = '3/5', distractor_answers = ARRAY['2/5', '12/8', '4/5']
WHERE topic_id IN (SELECT id FROM curriculum_topics WHERE topic_key = 'ratios')
  AND language_code = 'fr'
  AND correct_answer = '';

UPDATE curriculum_problems
SET correct_answer = '100 texts', distractor_answers = ARRAY['50 texts', '200 texts', '75 texts']
WHERE topic_id IN (SELECT id FROM curriculum_topics WHERE topic_key = 'linear_equations')
  AND language_code = 'en'
  AND correct_answer = '';

UPDATE curriculum_problems
SET correct_answer = '100 textos', distractor_answers = ARRAY['50 textos', '200 textos', '75 textos']
WHERE topic_id IN (SELECT id FROM curriculum_topics WHERE topic_key = 'linear_equations')
  AND language_code = 'fr'
  AND correct_answer = '';

UPDATE curriculum_problems
SET correct_answer = '$2.00', distractor_answers = ARRAY['$1.50', '$2.25', '$2.50']
WHERE topic_id IN (SELECT id FROM curriculum_topics WHERE topic_key = 'percentages')
  AND language_code = 'en'
  AND correct_answer = '';

UPDATE curriculum_problems
SET correct_answer = '2,00 $', distractor_answers = ARRAY['1,50 $', '2,25 $', '2,50 $']
WHERE topic_id IN (SELECT id FROM curriculum_topics WHERE topic_key = 'percentages')
  AND language_code = 'fr'
  AND correct_answer = '';
