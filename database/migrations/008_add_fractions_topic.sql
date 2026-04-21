-- 008_add_fractions_topic.sql
-- Adds Grade 6 Fractions topic (missing from initial seed) and aligns order_index.
-- Idempotent: safe to re-run.
--
-- Problem count:
--   EN: 8 problems at difficulty 1–3
--   FR: 8 problems at difficulty 1–3

-- ── 1. Insert topic (idempotent) ──────────────────────────────────────────────

INSERT INTO curriculum_topics (grade, subject, topic_key, display_name, order_index)
VALUES (6, 'mathematics', 'fractions', 'Fractions', 0)
ON CONFLICT (grade, topic_key) DO NOTHING;

-- ── 2. Fix order_index for all Grade 6 topics ─────────────────────────────────
-- Assign canonical sequence matching CurriculumData.cs.

UPDATE curriculum_topics SET order_index = 0 WHERE grade = 6 AND topic_key = 'fractions';
UPDATE curriculum_topics SET order_index = 1 WHERE grade = 6 AND topic_key = 'integers';
UPDATE curriculum_topics SET order_index = 2 WHERE grade = 6 AND topic_key = 'ratios';
UPDATE curriculum_topics SET order_index = 3 WHERE grade = 6 AND topic_key = 'percentages';
UPDATE curriculum_topics SET order_index = 4 WHERE grade = 6 AND topic_key = 'intro_algebra';

-- ── 3. Seed problems (skip if already present) ────────────────────────────────

DO $$
DECLARE fid UUID;
BEGIN
  SELECT id INTO fid FROM curriculum_topics WHERE topic_key = 'fractions' AND grade = 6;

  IF (SELECT COUNT(*) FROM curriculum_problems WHERE topic_id = fid) > 0 THEN
    RETURN;  -- already seeded
  END IF;

  -- EN difficulty 1-3
  INSERT INTO curriculum_problems (topic_id, language_code, difficulty, problem_text, solution_steps, correct_answer, distractor_answers) VALUES
    (fid,'en',1,'What is 1/2 + 1/4?',
      '["Find common denominator: LCM of 2 and 4 is 4","Convert: 1/2 = 2/4","Add numerators: 2/4 + 1/4 = 3/4"]',
      '3/4', ARRAY['1/6','2/6','1/3']),
    (fid,'en',1,'Simplify 6/8 to lowest terms.',
      '["Find GCF of 6 and 8: GCF = 2","Divide both by 2: 6/2=3 and 8/2=4","Simplified fraction is 3/4"]',
      '3/4', ARRAY['2/4','1/2','4/6']),
    (fid,'en',1,'What is 3/4 of 20?',
      '["Multiply 20 by the numerator: 20 x 3 = 60","Divide by denominator: 60 / 4 = 15"]',
      '15', ARRAY['12','18','5']),
    (fid,'en',2,'Sam ate 2/5 of a pizza. His friend ate 1/3. How much pizza did they eat altogether?',
      '["Common denominator: LCM of 5 and 3 is 15","Convert: 2/5 = 6/15 and 1/3 = 5/15","Add: 6/15 + 5/15 = 11/15"]',
      '11/15', ARRAY['3/8','3/15','7/15']),
    (fid,'en',2,'A recipe needs 3/4 cup of flour. If you make half the recipe, how much flour do you need?',
      '["Multiply 3/4 by 1/2","Numerators: 3 x 1 = 3","Denominators: 4 x 2 = 8","Answer: 3/8 cup"]',
      '3/8 cup', ARRAY['1/4 cup','3/4 cup','1/2 cup']),
    (fid,'en',2,'Which fraction is larger: 2/3 or 3/5?',
      '["Common denominator: LCM of 3 and 5 is 15","Convert: 2/3 = 10/15 and 3/5 = 9/15","Compare: 10/15 > 9/15, so 2/3 is larger"]',
      '2/3', ARRAY['3/5','They are equal','Cannot compare']),
    (fid,'en',3,'What is 5/6 - 1/4?',
      '["Common denominator: LCM of 6 and 4 is 12","Convert: 5/6 = 10/12 and 1/4 = 3/12","Subtract: 10/12 - 3/12 = 7/12"]',
      '7/12', ARRAY['4/12','4/2','1/3']),
    (fid,'en',3,'A class of 30 students has 2/5 boys. How many are girls?',
      '["Number of boys: 2/5 x 30 = 12","Girls: 30 - 12 = 18"]',
      '18', ARRAY['12','15','20']);

  -- FR difficulty 1-3
  INSERT INTO curriculum_problems (topic_id, language_code, difficulty, problem_text, solution_steps, correct_answer, distractor_answers) VALUES
    (fid,'fr',1,'Combien fait 1/2 + 1/4 ?',
      '["Trouver le PPCM de 2 et 4 : PPCM = 4","Convertir : 1/2 = 2/4","Additionner : 2/4 + 1/4 = 3/4"]',
      '3/4', ARRAY['1/6','2/6','1/3']),
    (fid,'fr',1,'Simplifie 6/8 a sa forme la plus simple.',
      '["Trouver le PGCD de 6 et 8 : PGCD = 2","Diviser par 2 : 6/2=3 et 8/2=4","Fraction simplifiee : 3/4"]',
      '3/4', ARRAY['2/4','1/2','4/6']),
    (fid,'fr',1,'Combien font 3/4 de 20 ?',
      '["Multiplier 20 par le numerateur : 20 x 3 = 60","Diviser par le denominateur : 60 / 4 = 15"]',
      '15', ARRAY['12','18','5']),
    (fid,'fr',2,'Samuel a mange 2/5 d''une pizza. Son ami en a mange 1/3. Quelle fraction ont-ils mangee en tout ?',
      '["Denominateur commun : PPCM de 5 et 3 est 15","Convertir : 2/5 = 6/15 et 1/3 = 5/15","Additionner : 6/15 + 5/15 = 11/15"]',
      '11/15', ARRAY['3/8','3/15','7/15']),
    (fid,'fr',2,'Une recette demande 3/4 de tasse de farine. Pour la moitie de la recette, combien de farine faut-il ?',
      '["Multiplier 3/4 par 1/2","Numerateurs : 3 x 1 = 3","Denominateurs : 4 x 2 = 8","Reponse : 3/8 de tasse"]',
      '3/8 de tasse', ARRAY['1/4 de tasse','3/4 de tasse','1/2 de tasse']),
    (fid,'fr',2,'Quelle fraction est la plus grande : 2/3 ou 3/5 ?',
      '["Denominateur commun : PPCM de 3 et 5 est 15","Convertir : 2/3 = 10/15 et 3/5 = 9/15","Comparer : 10/15 > 9/15, donc 2/3 est plus grand"]',
      '2/3', ARRAY['3/5','Elles sont egales','Impossible a comparer']),
    (fid,'fr',3,'Combien fait 5/6 - 1/4 ?',
      '["Denominateur commun : PPCM de 6 et 4 est 12","Convertir : 5/6 = 10/12 et 1/4 = 3/12","Soustraire : 10/12 - 3/12 = 7/12"]',
      '7/12', ARRAY['4/12','4/2','1/3']),
    (fid,'fr',3,'Dans une classe de 30 eleves, 2/5 sont des garcons. Combien sont des filles ?',
      '["Nombre de garcons : 2/5 x 30 = 12","Filles : 30 - 12 = 18"]',
      '18', ARRAY['12','15','20']);

END $$;
