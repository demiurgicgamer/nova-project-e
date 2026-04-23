#!/usr/bin/env python3
"""
Nova Arc Content Agent — Phase 2: Export Approved Arcs to JSON
===============================================================
Reads approved .md arc files and uses Claude to convert them into
structured JSON matching the Nova database schema.

Only processes topics where status = 'approved' in the curriculum YAML.
Draft files are ignored — you must explicitly approve in the YAML first.

Usage:
  # Export all approved topics for a grade
  python export_json.py --grade 6

  # Export approved topics for a specific subject
  python export_json.py --grade 6 --subject mathematics

  # Export a single topic
  python export_json.py --grade 6 --subject mathematics --topic fractions --language en

  # Dry run — show what would be exported
  python export_json.py --grade 6 --dry-run
"""

import argparse
import json
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

PROVIDER_DEFAULTS = {
    "anthropic": "claude-opus-4-5",
    "gemini":    "gemini-2.0-flash",
    "groq":      "llama-3.3-70b-versatile",
    "ollama":    "llama3.1",
}

# ── Paths ─────────────────────────────────────────────────────────────────────
CONTENT_DIR    = Path(__file__).parent
CURRICULUM_DIR = CONTENT_DIR / "curriculum"
ARCS_DIR       = CONTENT_DIR / "arcs"


# ── JSON schema prompt ────────────────────────────────────────────────────────

JSON_SCHEMA = """
{
  "meta": {
    "subject":        string,
    "topic_key":      string,
    "topic_display":  string,
    "grade":          integer,
    "region":         string,   // e.g. "canada", "india", "global"
    "language_code":  string,
    "status":         "approved",
    "review_flags":   [string]
  },
  "hook_stories": [
    {
      "id":           string,        // "hook_a", "hook_b", etc.
      "culture_hint": string,        // "universal", "canadian", "sports", etc.
      "text":         string,
      "closing_line": string
    }
  ],
  "concept": {
    "explanation_steps": [string],   // each step Nova explains in order
    "whiteboard_text":   string,     // what appears on the whiteboard
    "comprehension_check": string,   // the Stage 2 informal check question
    "reteach_angles":    [string]    // alternative explanations (min 2)
  },
  "problems": [
    {
      "id":                  string,  // "p_guided_1", "p_practice_2a", "p_capstone_1"
      "stage":               "guided" | "practice" | "capstone",
      "difficulty":          "easy" | "medium" | "medium_hard" | "hard" | "capstone",
      "text":                string,  // problem as Nova speaks it
      "answer":              string,
      "answer_explanation":  string,
      "mc_choices":          [string], // exactly 4 — correct answer FIRST (index 0)
      "correct_index":       0,        // always 0; runtime shuffles before display
      "common_misconception": string,
      "intervention_hints": {
        "level_1": string,
        "level_2": string,
        "level_3": string
      },
      "nova_guiding_question": string  // capstone only: single allowed hint between attempts
    }
  ]
}
"""

SYSTEM_PROMPT = f"""You are a JSON extraction agent for the Nova AI tutor system.

You receive a pedagogical arc in Markdown format and convert it into a
structured JSON object for database insertion.

Rules:
1. Extract ALL information from the markdown — do not invent or omit content.
2. Every problem must have exactly 4 mc_choices. Correct answer goes at index 0.
   (The runtime will shuffle before showing to the child.)
3. Every problem must have intervention_hints with level_1, level_2, level_3.
4. If the markdown does not have enough detail for a field, use an empty string.
   Never invent content to fill gaps.
5. Output ONLY valid JSON — no markdown fences, no comments, no explanation.
6. The review_flags array should contain any content gaps or issues you notice.

JSON schema to produce:
{JSON_SCHEMA}
"""


# ── Curriculum helpers ────────────────────────────────────────────────────────

def load_curriculum(grade: int) -> dict:
    path = CURRICULUM_DIR / f"grade_{grade}.yaml"
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def arc_md_path(grade: int, subject: str, topic_key: str, language: str) -> Path:
    return ARCS_DIR / f"grade_{grade}" / subject / topic_key / f"{language}.md"


def arc_json_path(grade: int, subject: str, topic_key: str, language: str) -> Path:
    return ARCS_DIR / f"grade_{grade}" / subject / topic_key / f"{language}.json"


def get_approved_topics(curriculum: dict, subject_filter=None,
                        topic_filter=None, language_filter=None) -> list[dict]:
    grade     = curriculum["grade"]
    region    = curriculum.get("region", "global")
    languages = curriculum["languages"]
    approved  = []

    for subj in curriculum["subjects"]:
        subj_key = subj["subject"]
        if subject_filter and subj_key != subject_filter:
            continue

        for topic in subj["topics"]:
            if topic_filter and topic["key"] != topic_filter:
                continue

            langs = [language_filter] if language_filter else languages
            for lang in langs:
                status   = topic.get("status", {}).get(lang, "pending")
                md_path  = arc_md_path(grade, subj_key, topic["key"], lang)
                json_path = arc_json_path(grade, subj_key, topic["key"], lang)

                if status != "approved":
                    continue
                if not md_path.exists():
                    print(f"  [WARN] {subj_key}/{topic['key']}/{lang} marked approved but .md not found")
                    continue
                if json_path.exists():
                    print(f"  [SKIP] {subj_key}/{topic['key']}/{lang} — .json already exists")
                    continue

                approved.append({
                    "grade":          grade,
                    "region":         region,
                    "subject":        subj_key,
                    "subject_display": subj["display"],
                    "topic_key":      topic["key"],
                    "topic_display":  topic["display"],
                    "language":       lang,
                    "md_path":        md_path,
                    "json_path":      json_path,
                })

    return approved


# ── Export ────────────────────────────────────────────────────────────────────

def _parse_json_response(raw: str) -> dict:
    raw = raw.strip()
    # Strip Qwen3 / reasoning model <think>...</think> blocks
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # Strip markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def _stamp_meta(json_data: dict, item: dict) -> None:
    json_data.setdefault("meta", {}).update({
        "subject":       item["subject"],
        "topic_key":     item["topic_key"],
        "topic_display": item["topic_display"],
        "grade":         item["grade"],
        "region":        item["region"],
        "language_code": item["language"],
        "status":        "approved",
    })


def _export_anthropic(item: dict, model: str) -> tuple[dict, dict]:
    import anthropic
    client  = anthropic.Anthropic()
    md_text = item["md_path"].read_text(encoding="utf-8")
    message = client.messages.create(
        model=model, max_tokens=8192, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Convert this arc to JSON:\n\n{md_text}"}],
    )
    json_data = _parse_json_response(message.content[0].text)
    _stamp_meta(json_data, item)
    usage = {
        "input_tokens":  message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "cost_usd": round(
            (message.usage.input_tokens  / 1_000_000 * 15) +
            (message.usage.output_tokens / 1_000_000 * 75), 4
        ),
    }
    return json_data, usage


def _export_gemini(item: dict, model: str) -> tuple[dict, dict]:
    import google.generativeai as genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("\n  ERROR: GEMINI_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)
    genai.configure(api_key=api_key)
    gemini  = genai.GenerativeModel(model_name=model, system_instruction=SYSTEM_PROMPT)
    md_text = item["md_path"].read_text(encoding="utf-8")
    resp    = gemini.generate_content(
        f"Convert this arc to JSON:\n\n{md_text}",
        generation_config=genai.types.GenerationConfig(max_output_tokens=8192),
    )
    json_data = _parse_json_response(resp.text)
    _stamp_meta(json_data, item)
    usage = {
        "input_tokens":  getattr(resp.usage_metadata, "prompt_token_count", 0),
        "output_tokens": getattr(resp.usage_metadata, "candidates_token_count", 0),
        "cost_usd":      0.0,
    }
    return json_data, usage


def _export_openai_compat(item: dict, model: str,
                           base_url: str, api_key: str) -> tuple[dict, dict]:
    from openai import OpenAI
    client  = OpenAI(base_url=base_url, api_key=api_key or "ollama")
    md_text = item["md_path"].read_text(encoding="utf-8")
    resp    = client.chat.completions.create(
        model=model, max_tokens=8192,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"Convert this arc to JSON:\n\n{md_text}"},
        ],
    )
    json_data = _parse_json_response(resp.choices[0].message.content)
    _stamp_meta(json_data, item)
    usage = {
        "input_tokens":  resp.usage.prompt_tokens     if resp.usage else 0,
        "output_tokens": resp.usage.completion_tokens if resp.usage else 0,
        "cost_usd":      0.0,
    }
    return json_data, usage


def _export_groq(item: dict, model: str) -> tuple[dict, dict]:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("\n  ERROR: GROQ_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)
    return _export_openai_compat(item, model,
                                  base_url="https://api.groq.com/openai/v1",
                                  api_key=api_key)


def _export_ollama(item: dict, model: str) -> tuple[dict, dict]:
    return _export_openai_compat(item, model,
                                  base_url="http://localhost:11434/v1",
                                  api_key="ollama")


EXPORTERS = {
    "anthropic": _export_anthropic,
    "gemini":    _export_gemini,
    "groq":      _export_groq,
    "ollama":    _export_ollama,
}


def export_to_json(item: dict, model: str, provider: str) -> tuple[dict, dict]:
    return EXPORTERS[provider](item, model)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Export approved arc .md files to .json for DB insertion."
    )
    parser.add_argument("--grade",    required=True, type=int)
    parser.add_argument("--subject",  help="Filter by subject key")
    parser.add_argument("--topic",    help="Filter by topic key")
    parser.add_argument("--language", choices=["en", "fr"])
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument(
        "--provider",
        choices=list(EXPORTERS.keys()),
        default="gemini",
        help="AI provider to use (default: gemini — free tier)",
    )
    parser.add_argument("--model", default=None,
                        help="Model override (default: provider's recommended model)")
    args = parser.parse_args()
    model = args.model or PROVIDER_DEFAULTS[args.provider]

    curriculum = load_curriculum(args.grade)
    approved   = get_approved_topics(
        curriculum,
        subject_filter=args.subject,
        topic_filter=args.topic,
        language_filter=args.language,
    )

    if not approved:
        print("\n  Nothing to export — no approved topics found.")
        print("  To approve: set status to 'approved' in curriculum/grade_N.yaml")
        return

    free_providers = {"gemini", "groq", "ollama"}
    cost_note = "FREE" if args.provider in free_providers else "PAID"
    print(f"\n  Grade {args.grade} — {len(approved)} approved arc(s) to export")
    print(f"  Provider: {args.provider} ({cost_note}) | Model: {model}")
    if args.dry_run:
        print("  DRY RUN\n")

    total_cost = 0.0

    for i, item in enumerate(approved, 1):
        label = f"{item['subject']}/{item['topic_key']}/{item['language']}"
        print(f"\n  [{i}/{len(approved)}] {label}")

        if args.dry_run:
            print(f"         -> would export: {item['json_path']}")
            continue

        print(f"         Converting to JSON...", end="", flush=True)

        try:
            json_data, usage = export_to_json(item, model, args.provider)
        except json.JSONDecodeError as e:
            print(f"\n         ERROR: Invalid JSON from {args.provider}: {e}")
            continue
        except Exception as e:
            print(f"\n         ERROR: {e}")
            continue

        item["json_path"].write_text(
            json.dumps(json_data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

        total_cost += usage["cost_usd"]
        flags = json_data.get("meta", {}).get("review_flags", [])

        print(f" done")
        print(f"         Saved : {item['json_path']}")
        cost_str = f"${usage['cost_usd']:.4f}" if usage["cost_usd"] > 0 else "free"
        print(f"         Tokens: {usage['input_tokens']} in / {usage['output_tokens']} out — {cost_str}")
        if flags:
            print(f"         Flags : {len(flags)} — review before DB insert")

    if not args.dry_run:
        print(f"\n  {'=' * 56}")
        print(f"  Done. {len(approved)} JSON file(s) exported.")
        total_str = f"~${total_cost:.4f} USD" if total_cost > 0 else "free"
        print(f"  Total cost: {total_str}")
        print(f"\n  NEXT STEP: python insert_arc.py --grade {args.grade}")
        print(f"  {'=' * 56}\n")


if __name__ == "__main__":
    main()
