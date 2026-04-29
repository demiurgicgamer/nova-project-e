#!/usr/bin/env python3
"""
Nova Arc Content — insert_arc.py
=================================
Reads approved arc JSON files and inserts them into PostgreSQL.

Per JSON file, three operations are performed (all idempotent):
  1. Upsert  curriculum_topics        — ensure topic row exists
  2. Upsert  topic_stories            — hook narratives (Stage 1)
  3. Upsert  curriculum_problems      — concept check + guided + practice + capstone

Safe to re-run. Uses ON CONFLICT DO UPDATE so re-running updates content
if the JSON was edited after the first insert.

Usage:
  python insert_arc.py --grade 6                           # all approved
  python insert_arc.py --grade 6 --subject mathematics
  python insert_arc.py --grade 6 --subject mathematics --topic fractions --language en
  python insert_arc.py --grade 6 --dry-run                 # preview only, no DB writes
"""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# ── Paths & env ───────────────────────────────────────────────────────────────
CONTENT_DIR = Path(__file__).parent
ARCS_DIR    = CONTENT_DIR / "arcs"

load_dotenv(CONTENT_DIR.parent / ".env")

# ── Difficulty text → int ─────────────────────────────────────────────────────
DIFFICULTY_MAP = {
    "easy":        1,
    "medium":      2,
    "medium_hard": 3,
    "hard":        4,
    "capstone":    5,
}

def map_difficulty(value) -> int:
    return DIFFICULTY_MAP.get(str(value).lower().strip(), 2)

# ── Curriculum topic ordering ─────────────────────────────────────────────────
TOPIC_ORDER = {
    # Mathematics
    "fractions":                    0,
    "integers":                     1,
    "ratios":                       2,
    "percentages":                  3,
    "algebra_basics":               4,
    "linear_equations":             5,
    "geometry_area_perimeter":      6,
    "probability":                  7,
    "data_and_graphs":              8,
    # Physics
    "forces_and_motion":            0,
    "simple_machines":              1,
    "electricity_basics":           2,
    "light_and_optics":             3,
    # Chemistry
    "matter_and_materials":         0,
    "mixtures_and_solutions":       1,
    "physical_vs_chemical_changes": 2,
    # Biology
    "cells_and_life":               0,
    "ecosystems":                   1,
    "human_body_systems":           2,
}

# ── DB connection ─────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        port     = int(os.getenv("POSTGRES_PORT", "5432")),
        dbname   = os.getenv("POSTGRES_DB",       "nova_db"),
        user     = os.getenv("POSTGRES_USER",     "nova_user"),
        password = os.getenv("POSTGRES_PASSWORD", "nova_password"),
    )

# ── File collection ───────────────────────────────────────────────────────────
def collect_files(grade, subject, topic, language) -> list[Path]:
    base = ARCS_DIR
    if grade:   base = base / f"grade_{grade}"
    if subject: base = base / subject
    if topic:   base = base / topic

    files = sorted(base.rglob("*.json"))
    if language:
        files = [f for f in files if f.stem == language]
    return files

# ── DB operations ─────────────────────────────────────────────────────────────

def upsert_topic(cur, meta: dict) -> str:
    """Upsert curriculum_topics row; return its UUID."""
    cur.execute("""
        INSERT INTO curriculum_topics
            (grade, subject, topic_key, display_name, order_index)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (grade, topic_key) DO UPDATE
            SET display_name = EXCLUDED.display_name,
                subject      = EXCLUDED.subject
        RETURNING id
    """, (
        meta["grade"],
        meta["subject"],
        meta["topic_key"],
        meta["topic_display"],
        TOPIC_ORDER.get(meta["topic_key"], 99),
    ))
    return cur.fetchone()[0]


def upsert_stories(cur, topic_id: str, data: dict) -> int:
    """Upsert hook_stories into topic_stories. Returns rows affected."""
    stories = data.get("hook_stories", [])
    lang    = data["meta"]["language_code"]
    count   = 0

    for i, story in enumerate(stories):
        text = (story.get("text") or "").strip()
        if not text:
            continue
        cur.execute("""
            INSERT INTO topic_stories
                (topic_id, language_code, culture_hint, story_text, closing_line, order_index)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (topic_id, language_code, culture_hint, order_index) DO UPDATE
                SET story_text   = EXCLUDED.story_text,
                    closing_line = EXCLUDED.closing_line
        """, (
            topic_id,
            lang,
            story.get("culture_hint", "global"),
            text,
            (story.get("closing_line") or "").strip(),
            i,
        ))
        count += 1

    return count


def upsert_concept_check(cur, topic_id: str, data: dict) -> int:
    """
    Insert the Stage 2 comprehension_check as a 'concept' stage problem.
    Stores full concept data (explanation steps, whiteboard, reteach) in
    solution_steps JSONB so Nova can reconstruct the concept lesson.
    Returns 1 if inserted, 0 if no comprehension_check found.
    """
    concept = data.get("concept", {})
    check   = (concept.get("comprehension_check") or "").strip()
    if not check:
        return 0

    lang     = data["meta"]["language_code"]
    solution = {
        "explanation_steps": concept.get("explanation_steps", []),
        "whiteboard_text":   concept.get("whiteboard_text", ""),
        "reteach_angles":    concept.get("reteach_angles", []),
    }

    cur.execute("""
        INSERT INTO curriculum_problems
            (topic_id, language_code, difficulty, stage,
             problem_text, solution_steps, correct_answer, distractor_answers, cultural_context)
        VALUES (%s, %s, 1, 'concept', %s, %s, '', '{}', 'concept_check')
        ON CONFLICT DO NOTHING
    """, (
        topic_id, lang, check,
        psycopg2.extras.Json(solution),
    ))
    return 1


def upsert_problems(cur, topic_id: str, data: dict) -> int:
    """
    Upsert guided / practice / capstone problems.
    Returns count of rows inserted or updated.
    """
    problems = data.get("problems", [])
    lang     = data["meta"]["language_code"]
    count    = 0

    for prob in problems:
        stage = (prob.get("stage") or "practice").strip()
        if stage not in ("guided", "practice", "capstone"):
            continue

        text = (prob.get("question") or prob.get("text") or "").strip()
        if not text:
            continue

        # Resolve correct answer and distractors from mc_choices
        mc_choices    = prob.get("mc_choices") or []
        correct_index = int(prob.get("correct_index") or 0)

        if mc_choices:
            correct_answer = (
                mc_choices[correct_index]
                if correct_index < len(mc_choices)
                else mc_choices[0]
            )
            distractors = [c for i, c in enumerate(mc_choices) if i != correct_index]
        else:
            # No MC — open-answer problem (concept check style)
            correct_answer = str(prob.get("answer") or "")
            distractors    = []

        solution = {
            "answer_explanation":    prob.get("answer_explanation", ""),
            "intervention_hints":    prob.get("intervention_hints", {}),
            "nova_guiding_question": prob.get("nova_guiding_question", ""),
            "common_misconception":  prob.get("common_misconception", ""),
        }

        diff = map_difficulty(prob.get("difficulty", "medium"))

        cur.execute("""
            INSERT INTO curriculum_problems
                (topic_id, language_code, difficulty, stage,
                 problem_text, solution_steps, correct_answer,
                 distractor_answers, cultural_context)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (
            topic_id, lang, diff, stage, text,
            psycopg2.extras.Json(solution),
            correct_answer,
            distractors,
            prob.get("id", ""),   # store JSON problem id in cultural_context
        ))
        count += cur.rowcount

    return count


# ── Per-file processor ────────────────────────────────────────────────────────

def process_file(path: Path, conn, dry_run: bool) -> dict:
    result = {
        "file":     str(path.relative_to(CONTENT_DIR)),
        "label":    "",
        "stories":  0,
        "concept":  0,
        "problems": 0,
        "error":    None,
    }

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        result["error"] = f"JSON parse: {e}"
        return result

    meta = data.get("meta", {})
    if not meta.get("topic_key"):
        result["error"] = "missing meta.topic_key"
        return result

    result["label"] = (
        f"{meta.get('subject','?')}/{meta.get('topic_key','?')}/{meta.get('language_code','?')}"
    )

    if dry_run:
        # Count without writing
        result["stories"]  = len([s for s in data.get("hook_stories", []) if s.get("text")])
        result["concept"]  = 1 if data.get("concept", {}).get("comprehension_check") else 0
        result["problems"] = len([p for p in data.get("problems", [])
                                  if p.get("stage") in ("guided", "practice", "capstone")
                                  and (p.get("question") or p.get("text"))])
        return result

    try:
        cur      = conn.cursor()
        topic_id = upsert_topic(cur, meta)
        result["stories"]  = upsert_stories(cur, topic_id, data)
        result["concept"]  = upsert_concept_check(cur, topic_id, data)
        result["problems"] = upsert_problems(cur, topic_id, data)
        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        result["error"] = str(e)

    return result


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_report(results: list[dict], dry_run: bool):
    mode = "DRY RUN" if dry_run else "INSERTED"

    total_stories  = sum(r["stories"]  for r in results)
    total_concept  = sum(r["concept"]  for r in results)
    total_problems = sum(r["problems"] for r in results)
    errors         = [r for r in results if r["error"]]

    print(f"\n{'='*60}")
    print(f"  insert_arc -- {mode}")
    print(f"{'='*60}")
    print(f"  Files processed : {len(results)}")
    print(f"  Hook stories    : {total_stories}")
    print(f"  Concept checks  : {total_concept}")
    print(f"  Problems        : {total_problems}")
    print(f"  Errors          : {len(errors)}")
    print()

    for r in results:
        if r["error"]:
            print(f"  ERROR  {r['label'] or r['file']}")
            print(f"         {r['error']}")
        else:
            print(
                f"  ok  {r['label']:<50} "
                f"stories={r['stories']} concept={r['concept']} problems={r['problems']}"
            )

    if errors:
        print(f"\n  {len(errors)} file(s) failed — see above.")
        sys.exit(1)

    if dry_run:
        print("  [DRY RUN] Nothing written. Re-run without --dry-run to insert.")
    else:
        print(f"  All done.")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Insert approved arc JSON files into the Nova database."
    )
    parser.add_argument("--grade",    type=int, help="Filter by grade (e.g. 6)")
    parser.add_argument("--subject",  type=str, help="Filter by subject folder")
    parser.add_argument("--topic",    type=str, help="Filter by topic folder")
    parser.add_argument("--language", type=str, help="Filter by language (en or fr)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Preview counts without writing to DB")
    args = parser.parse_args()

    files = collect_files(args.grade, args.subject, args.topic, args.language)
    if not files:
        print("No arc JSON files found matching the given filters.")
        sys.exit(1)

    print(f"\n  Found {len(files)} arc JSON file(s) to process.")

    conn = None
    if not args.dry_run:
        try:
            conn = get_conn()
        except Exception as e:
            print(f"\n  ERROR: Cannot connect to database — {e}")
            print("  Is Docker running? Check POSTGRES_* vars in .env")
            sys.exit(1)

    results = []
    for path in files:
        r = process_file(path, conn, dry_run=args.dry_run)
        results.append(r)

    if conn:
        conn.close()

    print_report(results, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
