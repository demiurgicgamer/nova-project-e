#!/usr/bin/env python3
"""
Nova Arc Content Agent — Phase 1: Generate Drafts
==================================================
Reads the curriculum master file (curriculum/grade_N.yaml) and generates
pedagogical arc .md files for each topic using ARC_FRAMEWORK.md as the law
and the reference arc as the quality benchmark.

PHASE 1 — This script (generate_arc.py)
  Generates .md draft files. Human reviews and approves them.
  Status in curriculum YAML updates to 'approved' when ready.

PHASE 2 — export_json.py
  Converts approved .md files to .json for database insertion.

Folder structure:
  arcs/
    grade_6/
      mathematics/
        fractions/
          en.md   ← draft or approved
          en.json ← exported after approval
          fr.md
          fr.json
        integers/
          en.md
          ...

Usage:
  # Single topic
  python generate_arc.py --grade 6 --subject mathematics --topic fractions --language en

  # All pending topics in a subject
  python generate_arc.py --grade 6 --subject mathematics

  # All pending topics across all subjects (use with care — costs API credits per topic)
  python generate_arc.py --grade 6 --all

  # Dry run — show what would be generated without calling the API
  python generate_arc.py --grade 6 --all --dry-run
"""

import argparse
import os
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

# ── Environment ───────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent.parent / ".env")
if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("CLAUDE_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = os.environ["CLAUDE_API_KEY"]

import anthropic  # noqa: E402 — import after env setup

# ── Paths ─────────────────────────────────────────────────────────────────────
CONTENT_DIR   = Path(__file__).parent
FRAMEWORK_MD  = CONTENT_DIR / "ARC_FRAMEWORK.md"
REFERENCE_ARC = CONTENT_DIR / "arcs" / "grade_6" / "mathematics" / "fractions" / "en.md"
CURRICULUM_DIR = CONTENT_DIR / "curriculum"
ARCS_DIR      = CONTENT_DIR / "arcs"


# ── Curriculum helpers ────────────────────────────────────────────────────────

def load_curriculum(grade: int) -> dict:
    path = CURRICULUM_DIR / f"grade_{grade}.yaml"
    if not path.exists():
        print(f"ERROR: Curriculum file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def arc_path(grade: int, subject: str, topic_key: str, language: str) -> Path:
    return ARCS_DIR / f"grade_{grade}" / subject / topic_key / f"{language}.md"


def get_pending_topics(curriculum: dict, subject_filter: str = None,
                       topic_filter: str = None, language_filter: str = None) -> list[dict]:
    """Return list of {grade, subject, topic, language} dicts that are still pending."""
    grade     = curriculum["grade"]
    languages = curriculum["languages"]
    pending   = []

    for subj in curriculum["subjects"]:
        if subject_filter and subj["key"] if "key" in subj else subj["subject"] != subject_filter:
            # Use 'subject' field (not 'key') in the yaml
            pass
        subj_key = subj["subject"]
        if subject_filter and subj_key != subject_filter:
            continue

        for topic in subj["topics"]:
            if topic_filter and topic["key"] != topic_filter:
                continue

            langs = [language_filter] if language_filter else languages
            for lang in langs:
                status = topic.get("status", {}).get(lang, "pending")
                if status == "pending":
                    # Also skip if the file already exists (in case status wasn't updated)
                    path = arc_path(grade, subj_key, topic["key"], lang)
                    if path.exists():
                        print(f"  [SKIP] {subj_key}/{topic['key']}/{lang} — file exists (update status in YAML)")
                        continue
                    pending.append({
                        "grade":    grade,
                        "subject":  subj_key,
                        "subject_display": subj["display"],
                        "topic_key": topic["key"],
                        "topic_display": topic["display"],
                        "description": topic.get("description", ""),
                        "prerequisites": topic.get("prerequisites", []),
                        "review_note": topic.get("review_note", None),
                        "language": lang,
                    })

    return pending


# ── Prompt builders ───────────────────────────────────────────────────────────

def build_system_prompt() -> str:
    framework = FRAMEWORK_MD.read_text(encoding="utf-8")
    reference = REFERENCE_ARC.read_text(encoding="utf-8")

    return f"""You are the Nova Arc Content Agent.

Your sole purpose is to generate complete, pedagogically sound topic arcs for
the Nova AI tutor system. Every arc you produce must strictly follow the ARC
FRAMEWORK. Use the reference arc as your quality and format benchmark.

══════════════════════════════════════════════════════════════
ARC FRAMEWORK (absolute rules — no exceptions)
══════════════════════════════════════════════════════════════
{framework}

══════════════════════════════════════════════════════════════
REFERENCE ARC (match this structure and quality exactly)
══════════════════════════════════════════════════════════════
{reference}

══════════════════════════════════════════════════════════════
OUTPUT FORMAT RULES
══════════════════════════════════════════════════════════════

Output the arc as a single markdown document.
Start with the metadata header, then each stage clearly labelled.
Match the reference arc structure exactly.

Begin the document with this exact header block (fill in the values):

# Arc Spec — [Topic Display] | [Subject Display] | Grade [N]
**Languages:** [EN or FR-CA]
**Status:** Draft — awaiting human review before DB insertion
**Framework:** See ARC_FRAMEWORK.md — all rules apply
**Prerequisites:** [list or None]
[**Review Note:** only if sensitive — include warning for reviewer]

Then produce all 5 stages following the reference arc format precisely.

At the very end of the document add a REVIEW FLAGS section:

## Review Flags
[List anything the human reviewer should check carefully.
 If nothing flagged, write: No flags — standard arc.]
"""


def build_user_prompt(item: dict) -> str:
    lang_name = (
        "English (Canadian context where applicable)"
        if item["language"] == "en"
        else "French Canadian (Quebec context, natural colloquial French for children aged 11-12)"
    )

    prereq_str = (
        ", ".join(item["prerequisites"]) if item["prerequisites"] else "None"
    )

    review_note = (
        f"\n⚠ SPECIAL NOTE FOR THIS TOPIC: {item['review_note']}"
        if item["review_note"] else ""
    )

    return f"""Generate a complete Nova pedagogical arc for:

Subject:      {item['subject_display']}
Topic:        {item['topic_display']}
Topic key:    {item['topic_key']}
Grade:        {item['grade']}
Language:     {lang_name} (code: {item['language']})
Prerequisites: {prereq_str}

Topic description (what the child will learn):
{item['description'].strip()}
{review_note}

Requirements:
- Follow ARC_FRAMEWORK.md exactly — all 5 stages, complete
- Match the quality and depth of the reference Fractions arc
- Age-appropriate language for Grade {item['grade']} children (ages {item['grade'] + 5}–{item['grade'] + 6})
- At least 2 hook stories with different cultural angles
- All problems must have exactly 4 MC choices with real common-misconception distractors
- Include intervention hints (Level 1, 2, 3) for every problem
- End with a Review Flags section

Generate the complete arc now.
"""


# ── API call ──────────────────────────────────────────────────────────────────

def generate_arc_md(item: dict, model: str) -> tuple[str, dict]:
    """Call Claude API and return (markdown_text, usage_dict)."""
    client = anthropic.Anthropic()

    message = client.messages.create(
        model=model,
        max_tokens=8192,
        system=build_system_prompt(),
        messages=[
            {"role": "user", "content": build_user_prompt(item)}
        ]
    )

    usage = {
        "input_tokens":  message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "cost_usd": round(
            (message.usage.input_tokens / 1_000_000 * 15) +
            (message.usage.output_tokens / 1_000_000 * 75),
            4
        )
    }

    return message.content[0].text, usage


# ── Save ──────────────────────────────────────────────────────────────────────

def save_arc(item: dict, content: str) -> Path:
    path = arc_path(item["grade"], item["subject"], item["topic_key"], item["language"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate Nova arc .md drafts from the curriculum master file."
    )
    parser.add_argument("--grade",    required=True, type=int)
    parser.add_argument("--subject",  help="Filter by subject key, e.g. mathematics")
    parser.add_argument("--topic",    help="Filter by topic key, e.g. fractions")
    parser.add_argument("--language", choices=["en", "fr"], help="Filter by language")
    parser.add_argument("--all",      action="store_true", help="Generate all pending topics")
    parser.add_argument("--dry-run",  action="store_true", help="Show what would run, no API calls")
    parser.add_argument("--model",    default="claude-opus-4-5")
    args = parser.parse_args()

    if not args.all and not args.subject and not args.topic:
        parser.error("Specify --subject, --topic, or --all")

    # Validate required files
    for f in [FRAMEWORK_MD, REFERENCE_ARC]:
        if not f.exists():
            print(f"ERROR: Required file not found: {f}", file=sys.stderr)
            sys.exit(1)

    curriculum = load_curriculum(args.grade)
    pending    = get_pending_topics(
        curriculum,
        subject_filter=args.subject,
        topic_filter=args.topic,
        language_filter=args.language,
    )

    if not pending:
        print("\n  Nothing to generate — all matching topics are already drafted or approved.")
        return

    print(f"\n  Grade {args.grade} — {len(pending)} topic(s) to generate")
    print(f"  Model: {args.model}")
    if args.dry_run:
        print("  DRY RUN — no API calls will be made\n")

    total_cost = 0.0

    for i, item in enumerate(pending, 1):
        label = f"{item['subject']}/{item['topic_key']}/{item['language']}"
        print(f"\n  [{i}/{len(pending)}] {label}")

        if args.dry_run:
            print(f"         -> would generate: {arc_path(item['grade'], item['subject'], item['topic_key'], item['language'])}")
            continue

        print(f"         Calling API...", end="", flush=True)

        try:
            content, usage = generate_arc_md(item, args.model)
        except Exception as e:
            print(f"\n         ERROR: {e}")
            continue

        path = save_arc(item, content)

        total_cost += usage["cost_usd"]
        print(f" done")
        print(f"         Saved : {path}")
        print(f"         Tokens: {usage['input_tokens']} in / {usage['output_tokens']} out — ${usage['cost_usd']:.4f}")

        # Check for review flags
        if "Review Flags" in content:
            flags_section = content.split("## Review Flags")[-1].strip()
            if "No flags" not in flags_section:
                print(f"         *** HAS REVIEW FLAGS — check before approving ***")

    if not args.dry_run and len(pending) > 0:
        print(f"\n  {'=' * 56}")
        print(f"  Done. {len(pending)} arc(s) generated.")
        print(f"  Total cost: ~${total_cost:.4f} USD")
        print(f"\n  NEXT STEPS:")
        print(f"  1. Review .md files in content/arcs/grade_{args.grade}/")
        print(f"  2. Update status to 'approved' in curriculum/grade_{args.grade}.yaml")
        print(f"  3. Run: python export_json.py --grade {args.grade}")
        print(f"  {'=' * 56}\n")


if __name__ == "__main__":
    main()
