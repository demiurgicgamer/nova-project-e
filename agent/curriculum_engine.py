"""
curriculum_engine.py — Topic and problem selection for Ms. Nova sessions.
Day 30: Grade-appropriate topic/problem selection with difficulty adaptation.

Responsibilities:
  - Query curriculum_topics + curriculum_problems from PostgreSQL
  - Select the best topic for this session (weak areas first, then unexplored, then any)
  - Pick a problem at the right difficulty, never repeating one seen this session
  - Build Socratic coaching context for Ms. Nova (what step to guide toward next)
  - Adapt difficulty after each problem based on accuracy

Integration:
  nova_agent.py calls this from node_select_pedagogy.
  Session curriculum state is stored in Redis: nova:session:{id}:curriculum
"""

import json
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Optional

import asyncpg

log = logging.getLogger("curriculum_engine")

DATABASE_URL = os.getenv("DATABASE_URL", "")


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Problem:
    id:                 str
    topic_key:          str
    topic_name:         str
    language:           str
    difficulty:         int
    text:               str
    steps:              list[str]
    context:            str       = ""
    correct_answer:     str       = ""
    distractor_answers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "Problem":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class CurriculumState:
    """
    Per-session curriculum state — serialised as JSON in Redis.

    Tracks which problem is active, how many hints have been given,
    and running accuracy so difficulty can be adjusted between problems.
    """
    topic_key:       str          = ""
    topic_name:      str          = ""
    problem_id:      str          = ""
    problem_text:    str          = ""
    solution_steps:  list[str]    = field(default_factory=list)
    difficulty:      int          = 2     # 1–5; start at medium-low
    hints_given:     int          = 0     # steps revealed for current problem
    problems_seen:   list[str]    = field(default_factory=list)  # IDs shown this session
    turn_count:      int          = 0     # total conversation turns this session
    session_correct:  int       = 0     # correct answers this session
    session_total:    int       = 0     # problems attempted this session
    question_choices: list[str] = field(default_factory=list)  # shuffled [A,B,C,D] texts
    question_correct: int       = -1   # index of correct choice in question_choices
    mc_answered:      bool      = False  # True after MC tap; prevents double-counting in select_pedagogy
    is_resuming:      bool      = False  # True when child has prior history on this topic (checkpoint OR DB mastery)

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "CurriculumState":
        fields = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in fields})

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def has_problem(self) -> bool:
        return bool(self.problem_id)

    @property
    def is_new_problem(self) -> bool:
        """True on the very first turn after a problem is loaded (hints_given == 0)."""
        return self.has_problem and self.hints_given == 0

    @property
    def next_hint(self) -> Optional[str]:
        """Next unrevealed solution step, or None when all steps are used."""
        idx = self.hints_given
        return self.solution_steps[idx] if idx < len(self.solution_steps) else None

    @property
    def chunk_phase(self) -> str:
        """
        Returns the current pedagogical phase based on problems completed.
        Drives the 4-dot progress strip in the Unity HUD.

        Using problems_seen (count of distinct problems attempted this session)
        rather than turn_count gives accurate, child-visible progress — one dot
        advances per problem, not per conversation exchange.

          intro       — 0 problems done  (dot 0)  warming up / first problem
          chunk_a     — 1 problem done   (dot 1)  building understanding
          chunk_b     — 2 problems done  (dot 2)  deepening practice
          consolidate — 3+ problems done (dot 3)  wrap-up / mastery check
        """
        done = max(0, len(self.problems_seen) - 1)  # -1: first problem is "intro" not "done"
        if done == 0:
            return "intro"
        elif done == 1:
            return "chunk_a"
        elif done == 2:
            return "chunk_b"
        else:
            return "consolidate"

    @property
    def is_exhausted(self) -> bool:
        """
        True when the student has had enough time with this problem.
        Either:
          - Hints have exceeded all steps + 2 buffer turns (verbal path), OR
          - An MC answer was submitted (mc_answered flag set by /answer endpoint)
        """
        return self.mc_answered or self.hints_given >= len(self.solution_steps) + 2

    def load_problem(self, problem: "Problem") -> None:
        self.topic_key      = problem.topic_key
        self.topic_name     = problem.topic_name
        self.problem_id     = problem.id
        self.problem_text   = problem.text
        self.solution_steps = problem.steps
        self.hints_given    = 0
        if problem.id not in self.problems_seen:
            self.problems_seen.append(problem.id)

        # Build shuffled multiple-choice choices for the question card
        if problem.correct_answer and len(problem.distractor_answers) >= 1:
            choices = list(problem.distractor_answers[:3])
            # Ensure we always have 4 choices (pad with placeholders if fewer distractors)
            while len(choices) < 3:
                choices.append("—")
            choices.append(problem.correct_answer)
            random.shuffle(choices)
            self.question_choices = choices
            self.question_correct = choices.index(problem.correct_answer)
        else:
            # No MC data — question card will stay hidden
            self.question_choices = []
            self.question_correct = -1


# ── CurriculumEngine ──────────────────────────────────────────────────────────

class CurriculumEngine:
    """
    Selects grade-appropriate topics and problems from the database.
    Adapts difficulty based on in-session performance.

    Usage (in nova_agent.py startup):
        engine = CurriculumEngine()
        await engine.init()
    """

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None

    async def init(self) -> None:
        """Create PostgreSQL connection pool. Call once at app startup."""
        if not DATABASE_URL:
            log.warning("[CurriculumEngine] DATABASE_URL not set — using fallback problems only.")
            return
        try:
            self._pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
            log.info("[CurriculumEngine] PostgreSQL pool ready.")
        except Exception as e:
            log.error(f"[CurriculumEngine] DB pool failed: {e}")
            self._pool = None

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    @staticmethod
    def _fallback_topic_key(grade: int) -> str:
        """Return the default topic key for a grade when no topic is cached yet."""
        return _FALLBACK_TOPICS.get(grade, _FALLBACK_TOPICS[6])["topic_key"]

    # ── Child progress queries ────────────────────────────────────────────────

    async def get_child_topic_progress(self, child_id: str, topic_key: str) -> dict:
        """
        Look up a child's recorded progress for a topic from the DB.
        Returns {"mastery_level": int, "attempt_count": int} or {} if not found / DB unavailable.

        mastery_level is 0–100.  attempt_count > 0 means the child has done at least
        one session on this topic, even if mastery is still low.
        """
        if not self._pool or not child_id or not topic_key:
            return {}
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT ctp.mastery_level, ctp.attempt_count
                    FROM   child_topic_progress ctp
                    JOIN   curriculum_topics    ct  ON ct.id = ctp.topic_id
                    WHERE  ctp.child_id = $1::uuid
                    AND    ct.topic_key = $2
                    LIMIT  1
                    """,
                    child_id, topic_key,
                )
            if row:
                return {
                    "mastery_level": row["mastery_level"],
                    "attempt_count": row["attempt_count"],
                }
        except Exception as e:
            log.warning(f"[CurriculumEngine] get_child_topic_progress error: {e}")
        return {}

    # ── Topic selection ───────────────────────────────────────────────────────

    async def select_topic(
        self,
        grade: int,
        language: str,
        weak_topics: list[str],
        covered_today: list[str],
    ) -> dict:
        """
        Select the most appropriate topic for this session.

        Priority:
          1. Weak topics not yet covered today   (address gaps, fresh start)
          2. Any weak topic                       (keep reviewing gaps)
          3. Any uncovered topic for this grade   (explore new content)
          4. Any topic for this grade             (repeat as needed)

        Returns {"topic_key": str, "topic_name": str}
        """
        if not self._pool:
            return _fallback_topic(grade)

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT ct.topic_key, ct.display_name, ct.order_index
                    FROM   curriculum_topics   ct
                    JOIN   curriculum_problems cp ON cp.topic_id = ct.id
                    WHERE  ct.grade      = $1
                    AND    ct.subject    = 'math'
                    AND    cp.language_code = $2
                    ORDER  BY ct.order_index
                    """,
                    grade, language,
                )

            if not rows:
                log.warning(f"[CurriculumEngine] No topics for grade {grade} / {language}")
                return _fallback_topic(grade)

            topics = [{"key": r["topic_key"], "name": r["display_name"]} for r in rows]

            # Priority 1: weak + not covered today
            for t in topics:
                if t["key"] in weak_topics and t["key"] not in covered_today:
                    log.info(f"[CurriculumEngine] Topic (weak, fresh): {t['key']}")
                    return {"topic_key": t["key"], "topic_name": t["name"]}

            # Priority 2: any weak topic
            for t in topics:
                if t["key"] in weak_topics:
                    log.info(f"[CurriculumEngine] Topic (weak): {t['key']}")
                    return {"topic_key": t["key"], "topic_name": t["name"]}

            # Priority 3: uncovered topic
            for t in topics:
                if t["key"] not in covered_today:
                    log.info(f"[CurriculumEngine] Topic (fresh): {t['key']}")
                    return {"topic_key": t["key"], "topic_name": t["name"]}

            # Priority 4: any topic
            chosen = random.choice(topics)
            log.info(f"[CurriculumEngine] Topic (repeat): {chosen['key']}")
            return {"topic_key": chosen["key"], "topic_name": chosen["name"]}

        except Exception as e:
            log.error(f"[CurriculumEngine] select_topic error: {e}")
            return _fallback_topic(grade)

    async def get_topic_by_key(self, topic_key: str, grade: int, language: str) -> dict:
        """
        Look up a specific topic by its key.
        Used when the child has already chosen a topic in the UI — skip the
        priority-selection algorithm and use exactly what they picked.

        Returns {"topic_key": str, "topic_name": str}.
        Falls back to a title-cased version of the key if the DB lookup fails.
        """
        fallback = {
            "topic_key":  topic_key,
            "topic_name": topic_key.replace("_", " ").title(),
        }

        if not self._pool:
            return fallback

        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT topic_key, display_name
                    FROM   curriculum_topics
                    WHERE  topic_key = $1
                    AND    grade     = $2
                    AND    subject   = 'math'
                    LIMIT  1
                    """,
                    topic_key, grade,
                )
            if row:
                return {"topic_key": row["topic_key"], "topic_name": row["display_name"]}
            log.warning(f"[CurriculumEngine] topic_key '{topic_key}' not found in DB — using fallback")
        except Exception as e:
            log.warning(f"[CurriculumEngine] get_topic_by_key error: {e}")

        return fallback

    # ── Problem selection ─────────────────────────────────────────────────────

    async def select_problem(
        self,
        topic_key: str,
        grade: int,
        language: str,
        difficulty: int,
        exclude_ids: list[str] | None = None,
    ) -> Optional[Problem]:
        """
        Select a problem at the target difficulty, never repeating an excluded ID.
        If the exact difficulty has no unseen problems, searches adjacent difficulty
        levels (closest first) until one is found.

        Returns None only if the database has no problems at all for this topic.
        Falls back to hardcoded problems if the DB is unavailable.
        """
        exclude_ids = exclude_ids or []

        if not self._pool:
            return _fallback_problem(topic_key, grade, language)

        try:
            async with self._pool.acquire() as conn:
                for target_diff in _difficulty_search_order(difficulty):
                    rows = await conn.fetch(
                        """
                        SELECT cp.id::text,
                               ct.topic_key, ct.display_name,
                               cp.language_code, cp.difficulty,
                               cp.problem_text, cp.solution_steps,
                               cp.cultural_context,
                               cp.correct_answer, cp.distractor_answers
                        FROM   curriculum_problems cp
                        JOIN   curriculum_topics   ct ON ct.id = cp.topic_id
                        WHERE  ct.topic_key     = $1
                        AND    ct.grade         = $2
                        AND    cp.language_code = $3
                        AND    cp.difficulty    = $4
                        ORDER  BY RANDOM()
                        LIMIT  20
                        """,
                        topic_key, grade, language, target_diff,
                    )

                    for row in rows:
                        pid = row["id"]
                        if pid in exclude_ids:
                            continue

                        steps = row["solution_steps"]
                        if isinstance(steps, str):
                            steps = json.loads(steps)
                        elif steps is None:
                            steps = []

                        distractors = row["distractor_answers"]
                        if distractors is None:
                            distractors = []
                        elif isinstance(distractors, str):
                            distractors = json.loads(distractors)

                        return Problem(
                            id                 = pid,
                            topic_key          = row["topic_key"],
                            topic_name         = row["display_name"],
                            language           = row["language_code"],
                            difficulty         = row["difficulty"],
                            text               = row["problem_text"],
                            steps              = steps,
                            context            = row["cultural_context"] or "",
                            correct_answer     = row["correct_answer"] or "",
                            distractor_answers = list(distractors),
                        )

            # All DB problems for this topic have been seen this session.
            # Rather than falling back to the same hardcoded problem every time,
            # query the full pool (ignoring exclude_ids) and pick one at random
            # so the student gets variety even after exhausting new material.
            log.warning(f"[CurriculumEngine] No unseen problems for {topic_key}/{grade}/{language} — recycling from full pool")
            async with self._pool.acquire() as conn:
                all_rows = await conn.fetch(
                    """
                    SELECT cp.id::text,
                           ct.topic_key, ct.display_name,
                           cp.language_code, cp.difficulty,
                           cp.problem_text, cp.solution_steps,
                           cp.cultural_context,
                           cp.correct_answer, cp.distractor_answers
                    FROM   curriculum_problems cp
                    JOIN   curriculum_topics   ct ON ct.id = cp.topic_id
                    WHERE  ct.topic_key     = $1
                    AND    ct.grade         = $2
                    AND    cp.language_code = $3
                    ORDER  BY RANDOM()
                    LIMIT  20
                    """,
                    topic_key, grade, language,
                )

            if all_rows:
                row = random.choice(all_rows)
                steps = row["solution_steps"]
                if isinstance(steps, str):
                    steps = json.loads(steps)
                elif steps is None:
                    steps = []
                distractors = row["distractor_answers"]
                if distractors is None:
                    distractors = []
                elif isinstance(distractors, str):
                    distractors = json.loads(distractors)
                log.info(f"[CurriculumEngine] Recycled problem id={row['id']} (pool exhausted)")
                return Problem(
                    id                 = row["id"],
                    topic_key          = row["topic_key"],
                    topic_name         = row["display_name"],
                    language           = row["language_code"],
                    difficulty         = row["difficulty"],
                    text               = row["problem_text"],
                    steps              = steps,
                    context            = row["cultural_context"] or "",
                    correct_answer     = row["correct_answer"] or "",
                    distractor_answers = list(distractors),
                )

            # DB truly has no problems at all for this topic — use hardcoded fallback
            log.warning(f"[CurriculumEngine] No DB problems at all for {topic_key}/{grade}/{language} — using hardcoded fallback")
            return _fallback_problem(topic_key, grade, language)

        except Exception as e:
            log.error(f"[CurriculumEngine] select_problem error: {e}")
            return _fallback_problem(topic_key, grade, language)

    # ── Difficulty adaptation ─────────────────────────────────────────────────

    @staticmethod
    def next_difficulty(
        session_correct: int,
        session_total: int,
        current: int,
    ) -> int:
        """
        Recommend a difficulty adjustment after each problem attempt.

        Up by 1:   accuracy ≥ 100% over last session (≥3 attempts)
        Down by 1: accuracy < 40% over ≥2 attempts
        Otherwise: hold
        Clamp to [1, 5].
        """
        if session_total < 1:
            return current
        accuracy = session_correct / session_total
        if accuracy >= 1.0 and session_total >= 3:
            return min(5, current + 1)
        if accuracy < 0.4 and session_total >= 2:
            return max(1, current - 1)
        return current

    # ── Question display builder ──────────────────────────────────────────────

    @staticmethod
    def build_question_data(cs: "CurriculumState") -> Optional[dict]:
        """
        Build the question_display payload sent to Unity's question card.

        Returns None if no choices are available (voice-only fallback).
        The choices are already shuffled when load_problem() is called.
        """
        if not cs.has_problem or not cs.question_choices or cs.question_correct < 0:
            return None
        return {
            "text":          cs.problem_text,
            "choices":       cs.question_choices,
            "correct_index": cs.question_correct,
        }

    # ── Socratic coaching context ─────────────────────────────────────────────

    @staticmethod
    def build_coaching_context(cs: CurriculumState, language: str) -> str:
        """
        Build the [Internal coaching] block injected into Ms. Nova's system prompt.

        This tells Nova:
          - What problem the student is working on
          - Which steps have already been guided
          - What to guide toward next (Socratically — never state it directly)

        The student never sees this block.
        """
        if not cs.has_problem:
            return ""

        next_hint = cs.next_hint
        revealed  = cs.solution_steps[: cs.hints_given]

        if language == "fr":
            lines = [
                "[Contexte pédagogique — usage interne uniquement, ne pas divulguer à l'élève]",
                f"Sujet : {cs.topic_name}",
                f"Problème actuel : {cs.problem_text}",
            ]
            if revealed:
                steps_str = " → ".join(f"Étape {i+1}: {s}" for i, s in enumerate(revealed))
                lines.append(f"Étapes déjà guidées : {steps_str}")
            if next_hint:
                lines.append(
                    f"Prochaine étape à guider (via questions Socratiques, "
                    f"NE PAS énoncer directement) : {next_hint}"
                )
            else:
                lines.append(
                    "L'élève a travaillé toutes les étapes. "
                    "Invitez-le/la à présenter sa solution complète."
                )
        else:
            lines = [
                "[Pedagogical context — internal use only, do NOT reveal to student]",
                f"Topic: {cs.topic_name}",
                f"Current problem: {cs.problem_text}",
            ]
            if revealed:
                steps_str = " → ".join(f"Step {i+1}: {s}" for i, s in enumerate(revealed))
                lines.append(f"Steps already guided through: {steps_str}")
            if next_hint:
                lines.append(
                    f"Next step to guide toward (via Socratic questions, "
                    f"do NOT state directly): {next_hint}"
                )
            else:
                lines.append(
                    "Student has worked through all steps. "
                    "Encourage them to present their full solution."
                )

        return "\n".join(lines)


# ── Fallback data (no DB connection) ─────────────────────────────────────────

_FALLBACK_TOPICS: dict[int, dict] = {
    6: {"topic_key": "fractions",        "topic_name": "Fractions"},
    7: {"topic_key": "linear_equations", "topic_name": "Linear Equations"},
}

_FALLBACK_PROBLEMS: dict[tuple, Problem] = {
    ("fractions", "en"): Problem(
        id="fb_frac_en", topic_key="fractions", topic_name="Fractions",
        language="en", difficulty=1,
        text="What is 1/2 + 1/4?",
        steps=[
            "Find a common denominator: LCM of 2 and 4 is 4",
            "Convert: 1/2 = 2/4",
            "Add numerators: 2/4 + 1/4 = 3/4",
        ],
        context="arithmetic",
        correct_answer="3/4",
        distractor_answers=["1/6", "2/6", "1/3"],
    ),
    ("fractions", "fr"): Problem(
        id="fb_frac_fr", topic_key="fractions", topic_name="Fractions",
        language="fr", difficulty=1,
        text="Combien fait 1/2 + 1/4 ?",
        steps=[
            "Trouver le PPCM de 2 et 4 : PPCM = 4",
            "Convertir : 1/2 = 2/4",
            "Additionner : 2/4 + 1/4 = 3/4",
        ],
        context="arithmetic",
        correct_answer="3/4",
        distractor_answers=["1/6", "2/6", "1/3"],
    ),
    ("integers", "en"): Problem(
        id="fb_int_en", topic_key="integers", topic_name="Integers",
        language="en", difficulty=1,
        text="What is (-5) + 3?",
        steps=[
            "Start at -5 on the number line",
            "Move 3 steps to the right",
            "Land on -2",
        ],
        context="number_line",
        correct_answer="-2",
        distractor_answers=["2", "-8", "8"],
    ),
    ("integers", "fr"): Problem(
        id="fb_int_fr", topic_key="integers", topic_name="Entiers",
        language="fr", difficulty=1,
        text="Combien fait (-5) + 3 ?",
        steps=[
            "Partir de -5 sur la droite numerique",
            "Avancer de 3 pas vers la droite",
            "Arriver a -2",
        ],
        context="number_line",
        correct_answer="-2",
        distractor_answers=["2", "-8", "8"],
    ),
    ("ratios", "en"): Problem(
        id="fb_ratios_en", topic_key="ratios", topic_name="Ratios and Rates",
        language="en", difficulty=2,
        text=(
            "A hockey team won 12 games and lost 8 games. "
            "What is the ratio of wins to total games played?"
        ),
        steps=[
            "Find total games: 12 + 8 = 20",
            "Write the win ratio: 12 out of 20",
            "Simplify: divide both by 4 to get 3/5",
        ],
        context="hockey",
        correct_answer="3/5",
        distractor_answers=["2/5", "12/8", "4/5"],
    ),
    ("ratios", "fr"): Problem(
        id="fb_ratios_fr", topic_key="ratios", topic_name="Ratios et taux",
        language="fr", difficulty=2,
        text=(
            "Une équipe de hockey a gagné 12 parties et en a perdu 8. "
            "Quel est le ratio de victoires par rapport aux parties jouées?"
        ),
        steps=[
            "Trouver le total : 12 + 8 = 20 parties",
            "Écrire le ratio : 12 sur 20",
            "Simplifier en divisant par 4 : 3/5",
        ],
        context="hockey",
        correct_answer="3/5",
        distractor_answers=["2/5", "12/8", "4/5"],
    ),
    ("linear_equations", "en"): Problem(
        id="fb_lineq_en", topic_key="linear_equations", topic_name="Linear Equations",
        language="en", difficulty=2,
        text=(
            "A cell phone plan costs $25 per month plus $0.10 per text message. "
            "Maya's bill was $35. How many text messages did she send?"
        ),
        steps=[
            "Set up equation: 25 + 0.10t = 35",
            "Subtract 25 from both sides: 0.10t = 10",
            "Divide both sides by 0.10: t = 100 texts",
        ],
        context="cell_phone",
        correct_answer="100 texts",
        distractor_answers=["50 texts", "200 texts", "75 texts"],
    ),
    ("linear_equations", "fr"): Problem(
        id="fb_lineq_fr", topic_key="linear_equations", topic_name="Équations linéaires",
        language="fr", difficulty=2,
        text=(
            "Un forfait téléphonique coûte 25 $ par mois plus 0,10 $ par texto. "
            "La facture de Maya était de 35 $. Combien de textos a-t-elle envoyés?"
        ),
        steps=[
            "Écrire l'équation : 25 + 0,10t = 35",
            "Soustraire 25 des deux côtés : 0,10t = 10",
            "Diviser par 0,10 : t = 100 textos",
        ],
        context="cell_phone",
        correct_answer="100 textos",
        distractor_answers=["50 textos", "200 textos", "75 textos"],
    ),
    ("percentages", "en"): Problem(
        id="fb_pct_en", topic_key="percentages", topic_name="Percentages",
        language="en", difficulty=2,
        text=(
            "A Tim Hortons muffin costs $2.50. During Roll Up the Rim, "
            "prices are discounted 20%. What is the sale price?"
        ),
        steps=[
            "Find the discount amount: 20% × $2.50 = $0.50",
            "Subtract from original: $2.50 − $0.50 = $2.00",
        ],
        context="tim_hortons",
        correct_answer="$2.00",
        distractor_answers=["$1.50", "$2.25", "$2.50"],
    ),
    ("percentages", "fr"): Problem(
        id="fb_pct_fr", topic_key="percentages", topic_name="Pourcentages",
        language="fr", difficulty=2,
        text=(
            "Un muffin chez Tim Hortons coûte 2,50 $. Pendant Roulez pour gagner, "
            "les prix sont réduits de 20 %. Quel est le prix de vente?"
        ),
        steps=[
            "Calculer la réduction : 20 % × 2,50 $ = 0,50 $",
            "Soustraire du prix original : 2,50 $ − 0,50 $ = 2,00 $",
        ],
        context="tim_hortons",
        correct_answer="2,00 $",
        distractor_answers=["1,50 $", "2,25 $", "2,50 $"],
    ),
}


def _fallback_topic(grade: int) -> dict:
    return _FALLBACK_TOPICS.get(grade, _FALLBACK_TOPICS[6])


def _fallback_problem(topic_key: str, grade: int, language: str) -> Optional[Problem]:
    # Exact match
    p = _FALLBACK_PROBLEMS.get((topic_key, language))
    if p:
        return p
    # Same topic, English
    p = _FALLBACK_PROBLEMS.get((topic_key, "en"))
    if p:
        return p
    # Grade default topic, same language
    default_key = _FALLBACK_TOPICS.get(grade, _FALLBACK_TOPICS[6])["topic_key"]
    p = _FALLBACK_PROBLEMS.get((default_key, language))
    if p:
        return p
    # Grade default topic, English
    return _FALLBACK_PROBLEMS.get((default_key, "en"))


def _difficulty_search_order(target: int) -> list[int]:
    """
    Return difficulty levels to query, in order of preference (closest to target first).
    E.g. target=3 → [3, 2, 4, 1, 5]
    """
    order = [target]
    for delta in range(1, 5):
        lower = target - delta
        upper = target + delta
        if lower >= 1:
            order.append(lower)
        if upper <= 5:
            order.append(upper)
    return order
