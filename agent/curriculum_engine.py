"""
curriculum_engine.py — Topic and problem selection for Ms. Nova sessions.

Responsibilities:
  - Query curriculum_topics + curriculum_problems from PostgreSQL
  - Select the best topic for this session (weak areas first, then unexplored, then any)
  - Pick a problem at the right difficulty AND arc stage, never repeating seen problems
  - Build Socratic coaching context for Ms. Nova (what step to guide toward next)
  - Adapt difficulty after each problem based on accuracy
  - Deliver the 5-stage arc: concept → guided → practice → capstone
  - Persist arc progress in DB arc_checkpoint table (resumes across sessions)

Integration:
  nova_agent.py calls CurriculumEngine from node_select_pedagogy and session endpoints.
  Session curriculum state is stored in Redis: nova:session:{id}:curriculum
  Arc progress is persisted in DB: arc_checkpoint (child_id, topic_id)
"""

import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import asyncpg

log = logging.getLogger("curriculum_engine")

DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── Arc stage ordering ────────────────────────────────────────────────────────

ARC_STAGES = ["concept", "guided", "practice", "capstone"]

def next_arc_stage(current: str) -> str:
    """Advance to the next arc stage. Stays at capstone if already there."""
    try:
        idx = ARC_STAGES.index(current)
    except ValueError:
        return "guided"
    return ARC_STAGES[min(idx + 1, len(ARC_STAGES) - 1)]


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
    stage:              str       = "practice"
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

    Arc delivery fields:
      arc_stage           — current stage in the 5-stage arc
      stage_problems_seen — problem IDs seen in the current arc stage
      stage_correct       — correct answers in the current stage
      stage_total         — problems attempted in the current stage
      warmup_pending      — True when child returns after 3+ day gap (serve 1 recap first)
    """
    topic_key:           str          = ""
    topic_name:          str          = ""
    problem_id:          str          = ""
    problem_text:        str          = ""
    solution_steps:      list[str]    = field(default_factory=list)
    difficulty:          int          = 2       # 1–5; start at medium-low
    hints_given:         int          = 0       # steps revealed for current problem
    problems_seen:       list[str]    = field(default_factory=list)
    turn_count:          int          = 0
    session_correct:     int          = 0
    session_total:       int          = 0
    question_choices:    list[str]    = field(default_factory=list)
    question_correct:    int          = -1
    mc_answered:         bool         = False
    is_resuming:         bool         = False

    # ── Arc delivery fields ───────────────────────────────────────────────────
    arc_stage:           str          = "concept"   # concept|guided|practice|capstone
    stage_problems_seen: list[str]    = field(default_factory=list)
    stage_correct:       int          = 0
    stage_total:         int          = 0
    warmup_pending:      bool         = False

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
        """
        done = max(0, len(self.problems_seen) - 1)
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
        """True when the student has had enough time with this problem."""
        return self.mc_answered or self.hints_given >= len(self.solution_steps) + 2

    @property
    def should_advance_stage(self) -> bool:
        """
        Returns True when the current arc stage has been completed and the
        student should move to the next stage.

        Thresholds:
          concept  → guided   : after 1 attempt (comprehension check answered)
          guided   → practice : after 1 correct OR 2+ problems seen
          practice → capstone : after 2+ correct OR 3+ problems attempted
          capstone            : no auto-advance (repeat harder problems)
        """
        stage = self.arc_stage
        if stage == "concept":
            return self.stage_total >= 1
        elif stage == "guided":
            return self.stage_correct >= 1 or len(self.stage_problems_seen) >= 2
        elif stage == "practice":
            return self.stage_correct >= 2 or self.stage_total >= 3
        return False   # capstone: no auto-advance

    def advance_stage(self) -> str:
        """
        Advance to the next arc stage and reset per-stage counters.
        Returns the new stage name.
        """
        new_stage = next_arc_stage(self.arc_stage)
        log.info(f"[CurriculumState] Arc stage: {self.arc_stage} → {new_stage}")
        self.arc_stage           = new_stage
        self.stage_problems_seen = []
        self.stage_correct       = 0
        self.stage_total         = 0
        return new_stage

    def load_problem(self, problem: "Problem") -> None:
        self.topic_key      = problem.topic_key
        self.topic_name     = problem.topic_name
        self.problem_id     = problem.id
        self.problem_text   = problem.text
        self.solution_steps = problem.steps
        self.hints_given    = 0

        if problem.id not in self.problems_seen:
            self.problems_seen.append(problem.id)
        if problem.id not in self.stage_problems_seen:
            self.stage_problems_seen.append(problem.id)

        # Build shuffled multiple-choice choices for the question card
        if problem.correct_answer and len(problem.distractor_answers) >= 1:
            choices = list(problem.distractor_answers[:3])
            while len(choices) < 3:
                choices.append("—")
            choices.append(problem.correct_answer)
            random.shuffle(choices)
            self.question_choices = choices
            self.question_correct = choices.index(problem.correct_answer)
        else:
            self.question_choices = []
            self.question_correct = -1


# ── Solution steps parser ─────────────────────────────────────────────────────

def _parse_steps(raw) -> list[str]:
    """
    Parse solution_steps from DB, which can be a list (old format) or a JSONB
    dict (new arc format with intervention_hints, explanation_steps, etc.).

    Old seed problems: list of strings ["step 1", "step 2", ...]
    New arc problems:  dict {"intervention_hints": [...], "answer_explanation": "..."}
    Concept problems:  dict {"explanation_steps": [...], "whiteboard_text": "..."}
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return [raw]
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(s) for s in raw if s]
    if isinstance(raw, dict):
        # Try keys in priority order
        for key in ("intervention_hints", "explanation_steps", "reteach_angles"):
            val = raw.get(key)
            if isinstance(val, list) and val:
                return [str(s) for s in val if s]
        # Fallback: collect all non-empty string values
        return [str(v) for v in raw.values() if isinstance(v, str) and v]
    return []


# ── CurriculumEngine ──────────────────────────────────────────────────────────

class CurriculumEngine:
    """
    Selects grade-appropriate topics and problems from the database.
    Delivers the 5-stage arc: concept → guided → practice → capstone.
    Persists arc progress in DB arc_checkpoint.
    """

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None

    async def init(self) -> None:
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
        return _FALLBACK_TOPICS.get(grade, _FALLBACK_TOPICS[6])["topic_key"]

    # ── Topic ID lookup ───────────────────────────────────────────────────────

    async def get_topic_id(self, topic_key: str, grade: int) -> Optional[str]:
        """Look up curriculum_topics.id (UUID) by topic_key + grade."""
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id::text FROM curriculum_topics WHERE topic_key = $1 AND grade = $2 LIMIT 1",
                    topic_key, grade,
                )
            return row["id"] if row else None
        except Exception as e:
            log.warning(f"[CurriculumEngine] get_topic_id error: {e}")
            return None

    # ── Arc checkpoint (DB) ───────────────────────────────────────────────────

    async def load_arc_checkpoint(self, child_id: str, topic_key: str, grade: int) -> dict:
        """
        Load arc progress from DB arc_checkpoint.
        Returns dict with: arc_stage, completed_stages, stage_stats, last_seen.
        Returns {} if no checkpoint exists (first time on this topic).
        """
        if not self._pool or not child_id or not topic_key:
            return {}
        topic_id = await self.get_topic_id(topic_key, grade)
        if not topic_id:
            return {}
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT current_stage, completed_stages, stage_stats,
                           last_seen, mastery_snapshot
                    FROM   arc_checkpoint
                    WHERE  child_id = $1::uuid
                    AND    topic_id = $2::uuid
                    """,
                    child_id, topic_id,
                )
            if row:
                return {
                    "arc_stage":        row["current_stage"],
                    "completed_stages": list(row["completed_stages"] or []),
                    "stage_stats":      dict(row["stage_stats"] or {}),
                    "last_seen":        row["last_seen"],
                    "mastery_snapshot": row["mastery_snapshot"],
                }
        except Exception as e:
            log.warning(f"[CurriculumEngine] load_arc_checkpoint error: {e}")
        return {}

    async def save_arc_checkpoint(
        self,
        child_id:   str,
        topic_key:  str,
        grade:      int,
        cs:         CurriculumState,
    ) -> None:
        """Upsert arc progress into DB arc_checkpoint."""
        if not self._pool or not child_id or not topic_key:
            return
        topic_id = await self.get_topic_id(topic_key, grade)
        if not topic_id:
            return

        # Track which stages have been completed
        completed = []
        for s in ARC_STAGES:
            if s == cs.arc_stage:
                break
            completed.append(s)

        stage_stats = {
            cs.arc_stage: {
                "problems_seen": len(cs.stage_problems_seen),
                "correct":       cs.stage_correct,
                "total":         cs.stage_total,
            }
        }
        mastery = min(100, (cs.session_correct * 100 // max(cs.session_total, 1)))

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO arc_checkpoint
                        (child_id, topic_id, current_stage, completed_stages,
                         stage_stats, last_seen, mastery_snapshot)
                    VALUES ($1::uuid, $2::uuid, $3, $4, $5, NOW(), $6)
                    ON CONFLICT (child_id, topic_id) DO UPDATE
                        SET current_stage   = EXCLUDED.current_stage,
                            completed_stages= EXCLUDED.completed_stages,
                            stage_stats     = EXCLUDED.stage_stats,
                            last_seen       = NOW(),
                            mastery_snapshot= EXCLUDED.mastery_snapshot
                    """,
                    child_id, topic_id,
                    cs.arc_stage,
                    completed,
                    json.dumps(stage_stats),
                    mastery,
                )
            log.info(
                f"[CurriculumEngine] arc_checkpoint saved — "
                f"child={child_id[:8]}, topic={topic_key}, stage={cs.arc_stage}"
            )
        except Exception as e:
            log.warning(f"[CurriculumEngine] save_arc_checkpoint error: {e}")

    # ── Hook story ────────────────────────────────────────────────────────────

    async def get_hook_story(
        self,
        topic_key:    str,
        grade:        int,
        language:     str,
        culture_hint: Optional[str] = None,
    ) -> Optional[str]:
        """
        Fetch a hook story from topic_stories for the given topic + language.
        Returns "story_text  closing_line" as a single string, or None.
        Prefers the culture_hint if provided; otherwise picks randomly.
        """
        if not self._pool:
            return None
        topic_id = await self.get_topic_id(topic_key, grade)
        if not topic_id:
            return None
        try:
            async with self._pool.acquire() as conn:
                if culture_hint:
                    rows = await conn.fetch(
                        """
                        SELECT story_text, closing_line
                        FROM   topic_stories
                        WHERE  topic_id      = $1::uuid
                        AND    language_code = $2
                        AND    culture_hint  = $3
                        ORDER  BY order_index
                        LIMIT  1
                        """,
                        topic_id, language, culture_hint,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT story_text, closing_line
                        FROM   topic_stories
                        WHERE  topic_id      = $1::uuid
                        AND    language_code = $2
                        ORDER  BY RANDOM()
                        LIMIT  1
                        """,
                        topic_id, language,
                    )
            if rows:
                row = rows[0]
                text   = (row["story_text"] or "").strip()
                closer = (row["closing_line"] or "").strip()
                if closer and closer not in text:
                    return f"{text}\n\n{closer}"
                return text
        except Exception as e:
            log.warning(f"[CurriculumEngine] get_hook_story error: {e}")
        return None

    # ── Child progress queries ────────────────────────────────────────────────

    async def get_child_topic_progress(self, child_id: str, topic_key: str) -> dict:
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
                    AND    cp.language_code = $2
                    ORDER  BY ct.order_index
                    """,
                    grade, language,
                )

            if not rows:
                log.warning(f"[CurriculumEngine] No topics for grade {grade} / {language}")
                return _fallback_topic(grade)

            topics = [{"key": r["topic_key"], "name": r["display_name"]} for r in rows]

            for t in topics:
                if t["key"] in weak_topics and t["key"] not in covered_today:
                    return {"topic_key": t["key"], "topic_name": t["name"]}
            for t in topics:
                if t["key"] in weak_topics:
                    return {"topic_key": t["key"], "topic_name": t["name"]}
            for t in topics:
                if t["key"] not in covered_today:
                    return {"topic_key": t["key"], "topic_name": t["name"]}

            chosen = random.choice(topics)
            return {"topic_key": chosen["key"], "topic_name": chosen["name"]}

        except Exception as e:
            log.error(f"[CurriculumEngine] select_topic error: {e}")
            return _fallback_topic(grade)

    async def get_topic_by_key(self, topic_key: str, grade: int, language: str) -> dict:
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
                    LIMIT  1
                    """,
                    topic_key, grade,
                )
            if row:
                return {"topic_key": row["topic_key"], "topic_name": row["display_name"]}
        except Exception as e:
            log.warning(f"[CurriculumEngine] get_topic_by_key error: {e}")
        return fallback

    # ── Problem selection ─────────────────────────────────────────────────────

    async def select_problem(
        self,
        topic_key:   str,
        grade:       int,
        language:    str,
        difficulty:  int,
        arc_stage:   str             = "practice",
        exclude_ids: list[str] | None = None,
    ) -> Optional["Problem"]:
        """
        Select a problem at the target arc stage + difficulty, skipping already-seen IDs.

        Search order:
          1. Requested stage × target difficulty
          2. Requested stage × adjacent difficulties (closest first)
          3. Any stage × target difficulty  (stage pool exhausted — fall through gracefully)
          4. Any stage × any difficulty     (last resort before hardcoded fallback)

        Returns None only if the DB is unavailable.
        """
        exclude_ids = exclude_ids or []

        if not self._pool:
            return _fallback_problem(topic_key, grade, language)

        try:
            async with self._pool.acquire() as conn:
                # Pass 1: target stage × difficulty search order
                for target_diff in _difficulty_search_order(difficulty):
                    rows = await conn.fetch(
                        """
                        SELECT cp.id::text,
                               ct.topic_key, ct.display_name,
                               cp.language_code, cp.difficulty, cp.stage,
                               cp.problem_text, cp.solution_steps,
                               cp.cultural_context,
                               cp.correct_answer, cp.distractor_answers
                        FROM   curriculum_problems cp
                        JOIN   curriculum_topics   ct ON ct.id = cp.topic_id
                        WHERE  ct.topic_key     = $1
                        AND    ct.grade         = $2
                        AND    cp.language_code = $3
                        AND    cp.stage         = $4
                        AND    cp.difficulty    = $5
                        ORDER  BY RANDOM()
                        LIMIT  20
                        """,
                        topic_key, grade, language, arc_stage, target_diff,
                    )
                    for row in rows:
                        if row["id"] not in exclude_ids:
                            return _build_problem(row)

                # Pass 2: stage exhausted — try any difficulty in same stage
                log.info(
                    f"[CurriculumEngine] No unseen problems at stage={arc_stage} for "
                    f"{topic_key}/{grade}/{language} — trying all difficulties in stage"
                )
                rows = await conn.fetch(
                    """
                    SELECT cp.id::text,
                           ct.topic_key, ct.display_name,
                           cp.language_code, cp.difficulty, cp.stage,
                           cp.problem_text, cp.solution_steps,
                           cp.cultural_context,
                           cp.correct_answer, cp.distractor_answers
                    FROM   curriculum_problems cp
                    JOIN   curriculum_topics   ct ON ct.id = cp.topic_id
                    WHERE  ct.topic_key     = $1
                    AND    ct.grade         = $2
                    AND    cp.language_code = $3
                    AND    cp.stage         = $4
                    ORDER  BY RANDOM()
                    LIMIT  20
                    """,
                    topic_key, grade, language, arc_stage,
                )
                for row in rows:
                    if row["id"] not in exclude_ids:
                        return _build_problem(row)

                # Pass 3: entire stage pool seen this session — recycle within stage
                if rows:
                    row = random.choice(rows)
                    log.info(
                        f"[CurriculumEngine] Recycling problem in stage={arc_stage} "
                        f"(all seen): {row['id'][:8]}"
                    )
                    return _build_problem(row)

                # Pass 4: no problems at all in this stage — fall through to any stage
                log.warning(
                    f"[CurriculumEngine] No problems for stage={arc_stage}, "
                    f"{topic_key}/{grade}/{language} — falling back to any stage"
                )
                all_rows = await conn.fetch(
                    """
                    SELECT cp.id::text,
                           ct.topic_key, ct.display_name,
                           cp.language_code, cp.difficulty, cp.stage,
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
                for row in all_rows:
                    if row["id"] not in exclude_ids:
                        return _build_problem(row)
                if all_rows:
                    return _build_problem(random.choice(all_rows))

            log.warning(
                f"[CurriculumEngine] No DB problems at all for {topic_key}/{grade}/{language}"
            )
            return _fallback_problem(topic_key, grade, language)

        except Exception as e:
            log.error(f"[CurriculumEngine] select_problem error: {e}")
            return _fallback_problem(topic_key, grade, language)

    # ── Difficulty adaptation ─────────────────────────────────────────────────

    @staticmethod
    def next_difficulty(session_correct: int, session_total: int, current: int) -> int:
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
        if not cs.has_problem or not cs.question_choices or cs.question_correct < 0:
            return None
        return {
            "text":          cs.problem_text,
            "choices":       cs.question_choices,
            "correct_index": cs.question_correct,
        }

    # ── Socratic coaching context ─────────────────────────────────────────────

    @staticmethod
    def build_coaching_context(cs: "CurriculumState", language: str) -> str:
        """
        Build the [Internal coaching] block injected into Ms. Nova's system prompt.
        Includes arc stage context so Nova's tone matches the current phase.
        """
        if not cs.has_problem:
            return ""

        next_hint = cs.next_hint
        revealed  = cs.solution_steps[: cs.hints_given]

        # Arc stage coaching prefix
        if language == "fr":
            stage_prefix = {
                "concept":  "[Phase : Explication du concept — présente clairement, utilise le tableau blanc, pose la question de compréhension.]",
                "guided":   "[Phase : Pratique guidée — guide l'élève étape par étape avec des questions Socratiques. Scaffolding fort.]",
                "practice": "[Phase : Pratique autonome — coaching léger seulement. Laisse l'élève essayer seul avant d'offrir de l'aide.]",
                "capstone": "[Phase : Évaluation finale — observe et évalue. Guidance minimale — l'élève doit démontrer sa maîtrise.]",
            }.get(cs.arc_stage, "")
        else:
            stage_prefix = {
                "concept":  "[Stage: Concept Explanation — introduce clearly, use whiteboard, then ask the comprehension check.]",
                "guided":   "[Stage: Guided Practice — walk through step-by-step with Socratic questions. Heavy scaffolding.]",
                "practice": "[Stage: Independent Practice — light coaching only. Let the student attempt before offering hints.]",
                "capstone": "[Stage: Capstone Assessment — observe and assess. Minimal guidance — student must demonstrate mastery.]",
            }.get(cs.arc_stage, "")

        if language == "fr":
            lines = [
                stage_prefix,
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
                stage_prefix,
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

        return "\n".join(line for line in lines if line)


# ── Problem builder helper ────────────────────────────────────────────────────

def _build_problem(row) -> "Problem":
    """Build a Problem object from an asyncpg row dict."""
    steps = _parse_steps(row["solution_steps"])

    distractors = row["distractor_answers"]
    if distractors is None:
        distractors = []
    elif isinstance(distractors, str):
        try:
            distractors = json.loads(distractors)
        except Exception:
            distractors = []

    return Problem(
        id                 = str(row["id"]),
        topic_key          = row["topic_key"],
        topic_name         = row["display_name"],
        language           = row["language_code"],
        difficulty         = row["difficulty"],
        stage              = row.get("stage", "practice"),
        text               = row["problem_text"],
        steps              = steps,
        context            = row["cultural_context"] or "",
        correct_answer     = row["correct_answer"] or "",
        distractor_answers = list(distractors),
    )


# ── Fallback data (no DB connection) ─────────────────────────────────────────

_FALLBACK_TOPICS: dict[int, dict] = {
    6: {"topic_key": "fractions",        "topic_name": "Fractions"},
    7: {"topic_key": "linear_equations", "topic_name": "Linear Equations"},
}

_FALLBACK_PROBLEMS: dict[tuple, Problem] = {
    ("fractions", "en"): Problem(
        id="fb_frac_en", topic_key="fractions", topic_name="Fractions",
        language="en", difficulty=1, stage="guided",
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
        language="fr", difficulty=1, stage="guided",
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
        language="en", difficulty=1, stage="guided",
        text="What is (-5) + 3?",
        steps=["Start at -5 on the number line", "Move 3 steps to the right", "Land on -2"],
        context="number_line",
        correct_answer="-2",
        distractor_answers=["2", "-8", "8"],
    ),
    ("integers", "fr"): Problem(
        id="fb_int_fr", topic_key="integers", topic_name="Entiers",
        language="fr", difficulty=1, stage="guided",
        text="Combien fait (-5) + 3 ?",
        steps=["Partir de -5 sur la droite numérique", "Avancer de 3 pas vers la droite", "Arriver à -2"],
        context="number_line",
        correct_answer="-2",
        distractor_answers=["2", "-8", "8"],
    ),
    ("ratios", "en"): Problem(
        id="fb_ratios_en", topic_key="ratios", topic_name="Ratios and Rates",
        language="en", difficulty=2, stage="practice",
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
        language="fr", difficulty=2, stage="practice",
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
        language="en", difficulty=2, stage="practice",
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
        language="fr", difficulty=2, stage="practice",
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
        language="en", difficulty=2, stage="practice",
        text=(
            "A Tim Hortons muffin costs $2.50. During Roll Up the Rim, "
            "prices are discounted 20%. What is the sale price?"
        ),
        steps=["Find the discount amount: 20% × $2.50 = $0.50", "Subtract from original: $2.50 − $0.50 = $2.00"],
        context="tim_hortons",
        correct_answer="$2.00",
        distractor_answers=["$1.50", "$2.25", "$2.50"],
    ),
    ("percentages", "fr"): Problem(
        id="fb_pct_fr", topic_key="percentages", topic_name="Pourcentages",
        language="fr", difficulty=2, stage="practice",
        text=(
            "Un muffin chez Tim Hortons coûte 2,50 $. Pendant Roulez pour gagner, "
            "les prix sont réduits de 20 %. Quel est le prix de vente?"
        ),
        steps=["Calculer la réduction : 20 % × 2,50 $ = 0,50 $", "Soustraire du prix original : 2,50 $ − 0,50 $ = 2,00 $"],
        context="tim_hortons",
        correct_answer="2,00 $",
        distractor_answers=["1,50 $", "2,25 $", "2,50 $"],
    ),
}


def _fallback_topic(grade: int) -> dict:
    return _FALLBACK_TOPICS.get(grade, _FALLBACK_TOPICS[6])


def _fallback_problem(topic_key: str, grade: int, language: str) -> Optional[Problem]:
    p = _FALLBACK_PROBLEMS.get((topic_key, language))
    if p:
        return p
    p = _FALLBACK_PROBLEMS.get((topic_key, "en"))
    if p:
        return p
    default_key = _FALLBACK_TOPICS.get(grade, _FALLBACK_TOPICS[6])["topic_key"]
    p = _FALLBACK_PROBLEMS.get((default_key, language))
    if p:
        return p
    return _FALLBACK_PROBLEMS.get((default_key, "en"))


def _difficulty_search_order(target: int) -> list[int]:
    """Return difficulty levels to query, in order of preference (closest to target first)."""
    order = [target]
    for delta in range(1, 5):
        lower = target - delta
        upper = target + delta
        if lower >= 1:
            order.append(lower)
        if upper <= 5:
            order.append(upper)
    return order
