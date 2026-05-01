#!/usr/bin/env python3
"""
fix_grade7_arcs.py
==================
Two-pass repair for Grade 7 arc JSON files after validation.

Pass 1 — auto-fix (deterministic, no LLM needed):
  - correct_index != 0  -> rotate mc_choices so correct answer is first, set index = 0
  - intervention_hints is list -> convert to {level_1, level_2, level_3}
  - stage == 'challenge' -> 'capstone'

Pass 2 — re-export (LLM needed):
  Files that are empty, have 0 mc_choices, or have empty intervention_hints
  get their JSON deleted so export_json.py will re-process them.
  Then export_json.py is called automatically for those files.

Usage:
  python fix_grade7_arcs.py                          # fix + re-export with default ollama
  python fix_grade7_arcs.py --provider gemini        # use gemini for re-export
  python fix_grade7_arcs.py --fix-only               # pass 1 only, no re-export
  python fix_grade7_arcs.py --reexport-only          # pass 2 only, skip in-place fixes
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

CONTENT_DIR = Path(__file__).parent
ARCS_DIR    = CONTENT_DIR / "arcs" / "grade_7"

# ── Files that need full re-export ────────────────────────────────────────────
# These are broken beyond in-place repair: empty, 0 mc_choices, bad structure.
REEXPORT = []  # all re-exports done — pass1 normaliser handles remaining schema issues


def fix_wrong_field_names(problems: list, label: str) -> int:
    """
    qwen3 sometimes outputs non-standard field names. Normalise to schema:
      mc_options / options / choices → mc_choices
      correct_answer (string or letter "B") → correct_index (int, rotate to 0)
      between_attempts_hint / hint / hints → intervention_hints dict
    Returns number of problems fixed.
    """
    LETTER_TO_IDX = {"A": 0, "B": 1, "C": 2, "D": 3}
    fixed = 0

    for p in problems:
        changed = False

        # ── Normalise mc_choices field name ──────────────────────────────────
        for alt_key in ("mc_options", "options", "choices", "answers"):
            if alt_key in p and not p.get("mc_choices"):
                p["mc_choices"] = p.pop(alt_key)
                changed = True
                break

        # ── Normalise correct_answer → correct_index ─────────────────────────
        if "correct_answer" in p:
            correct_str = str(p.pop("correct_answer")).strip()
            mc = p.get("mc_choices") or []

            # Case 1: single letter "B" / "B." / "b"
            letter = correct_str.upper().rstrip(".")
            if letter in LETTER_TO_IDX:
                matched_idx = LETTER_TO_IDX[letter]
            else:
                # Case 2: try substring match against mc_choices
                matched_idx = None
                for i, choice in enumerate(mc):
                    if correct_str.lower() in choice.lower() or choice.lower() in correct_str.lower():
                        matched_idx = i
                        break
                if matched_idx is None:
                    matched_idx = 0  # fallback: assume first is correct

            if mc and matched_idx < len(mc) and matched_idx != 0:
                correct = mc[matched_idx]
                rest = [c for i, c in enumerate(mc) if i != matched_idx]
                p["mc_choices"] = [correct] + rest
            p["correct_index"] = 0
            changed = True

        # Ensure correct_index exists
        if "correct_index" not in p:
            p["correct_index"] = 0
            changed = True

        # ── Normalise intervention_hints field name / structure ───────────────
        hints = p.get("intervention_hints")
        if not hints:
            # Try alternate field names
            for alt_key in ("hints", "hint", "between_attempts_hint",
                            "intervention", "scaffolding_hints"):
                if alt_key in p:
                    raw = p.pop(alt_key)
                    if isinstance(raw, str):
                        p["intervention_hints"] = {
                            "level_1": raw, "level_2": raw, "level_3": raw}
                    elif isinstance(raw, list):
                        padded = (raw + ["", "", ""])[:3]
                        p["intervention_hints"] = {
                            "level_1": padded[0],
                            "level_2": padded[1],
                            "level_3": padded[2],
                        }
                    elif isinstance(raw, dict):
                        p["intervention_hints"] = raw
                    changed = True
                    break

        if changed:
            fixed += 1
            print(f"    normalised field names on [{p.get('id','?')}]")
    return fixed


def fix_correct_index(problems: list, label: str) -> int:
    """
    If correct_index != 0, rotate mc_choices so the correct answer
    moves to position 0, then set correct_index = 0.
    Returns number of problems fixed.
    """
    fixed = 0
    for p in problems:
        cidx = p.get("correct_index", 0)
        mc   = p.get("mc_choices") or []
        if not isinstance(cidx, int):
            try:
                cidx = int(cidx)
            except (ValueError, TypeError):
                continue
        if cidx != 0 and mc and cidx < len(mc):
            correct = mc[cidx]
            rest    = [c for i, c in enumerate(mc) if i != cidx]
            p["mc_choices"]    = [correct] + rest
            p["correct_index"] = 0
            fixed += 1
            print(f"    fixed correct_index on [{p.get('id','?')}]: was {cidx}, moved '{correct[:40]}' to index 0")
    return fixed


def fix_hints_array(problems: list, label: str) -> int:
    """
    If intervention_hints is a list, convert to {level_1, level_2, level_3}.
    Returns number of problems fixed.
    """
    fixed = 0
    for p in problems:
        hints = p.get("intervention_hints")
        if isinstance(hints, list):
            padded = (hints + ["", "", ""])[:3]
            p["intervention_hints"] = {
                "level_1": padded[0],
                "level_2": padded[1],
                "level_3": padded[2],
            }
            fixed += 1
            print(f"    converted hints list -> dict on [{p.get('id','?')}]")
    return fixed


def fix_invalid_stages(problems: list, label: str) -> int:
    """
    Rename non-standard stage values to the closest valid stage.
    'challenge' -> 'capstone'
    'assessment' -> 'capstone'
    Returns number fixed.
    """
    STAGE_MAP = {
        "challenge":    "capstone",
        "assessment":   "capstone",
        "comprehension_check": "guided",
    }
    fixed = 0
    for p in problems:
        stage = p.get("stage", "")
        if stage in STAGE_MAP:
            p["stage"] = STAGE_MAP[stage]
            fixed += 1
            print(f"    fixed stage on [{p.get('id','?')}]: '{stage}' -> '{p['stage']}'")
    return fixed


def normalise_problem(raw: dict, default_stage: str) -> dict:
    """Convert a raw dict (any field names) into a canonical problem dict."""
    LETTER_TO_IDX = {"A": 0, "B": 1, "C": 2, "D": 3}

    # Collect mc_choices from any variant field name
    mc = (raw.get("mc_choices") or raw.get("options") or
          raw.get("mc_options") or raw.get("choices") or raw.get("answers") or [])

    # Collect correct answer
    correct_raw = str(raw.get("correct_answer", raw.get("correct", "")) or "").strip()
    letter = correct_raw.upper().rstrip(".")
    if letter in LETTER_TO_IDX:
        cidx = LETTER_TO_IDX[letter]
    else:
        # Substring match
        cidx = 0
        for i, c in enumerate(mc):
            if correct_raw and correct_raw.lower() in c.lower():
                cidx = i
                break

    # Rotate mc so correct is at index 0
    if mc and cidx != 0 and cidx < len(mc):
        correct = mc[cidx]
        mc = [correct] + [c for i, c in enumerate(mc) if i != cidx]

    # Hints
    hints_raw = (raw.get("intervention_hints") or raw.get("hints") or
                 raw.get("interventions") or raw.get("hint") or
                 raw.get("between_attempts_hint") or [])
    if isinstance(hints_raw, str):
        hints_dict = {"level_1": hints_raw, "level_2": hints_raw, "level_3": hints_raw}
    elif isinstance(hints_raw, list):
        padded = (list(hints_raw) + ["", "", ""])[:3]
        hints_dict = {"level_1": padded[0], "level_2": padded[1], "level_3": padded[2]}
    elif isinstance(hints_raw, dict):
        hints_dict = hints_raw
    else:
        hints_dict = {"level_1": "", "level_2": "", "level_3": ""}

    stage = raw.get("stage", default_stage)
    STAGE_MAP = {"challenge": "capstone", "assessment": "capstone",
                 "comprehension_check": "guided"}
    stage = STAGE_MAP.get(stage, stage)

    return {
        "id":                   raw.get("id", "p_unknown"),
        "stage":                stage,
        "difficulty":           raw.get("difficulty", "medium"),
        "text":                 (raw.get("text") or raw.get("question") or
                                 raw.get("problem") or ""),
        "answer":               (raw.get("answer") or raw.get("solution") or
                                 mc[0] if mc else ""),
        "answer_explanation":   (raw.get("answer_explanation") or
                                 raw.get("explanation") or ""),
        "mc_choices":           mc,
        "correct_index":        0,
        "common_misconception": raw.get("common_misconception", ""),
        "intervention_hints":   hints_dict,
    }


def fix_misplaced_problems(data: dict, label: str) -> int:
    """
    qwen3 sometimes puts problems in top-level keys like 'practice', 'guided',
    'challenge', 'practice_problems', 'culminating_problem' instead of 'problems'.
    Collect them all and merge into data['problems'].
    Returns number of problems rescued.
    """
    # Mapping: top-level key → default stage for problems found there
    KEY_STAGE_MAP = {
        "guided":               "guided",
        "guided_problems":      "guided",
        "practice":             "practice",
        "practice_problems":    "practice",
        "capstone":             "capstone",
        "challenge":            "capstone",
        "culminating_problem":  "capstone",
        "capstone_problem":     "capstone",
        "assessment":           "capstone",
    }

    rescued = []
    for key, stage in KEY_STAGE_MAP.items():
        val = data.get(key)
        if not val:
            continue
        if isinstance(val, dict):
            val = [val]
        if isinstance(val, list):
            for raw in val:
                if isinstance(raw, dict):
                    rescued.append(normalise_problem(raw, stage))
            del data[key]

    if not rescued:
        return 0

    existing = data.setdefault("problems", [])
    existing.extend(rescued)
    print(f"    rescued {len(rescued)} problem(s) from non-standard top-level keys")
    return len(rescued)


def pass1_auto_fix(dry_run: bool = False) -> int:
    """
    In-place fixes for all Grade 7 JSON files.
    Returns total number of problems fixed.
    """
    reexport_paths = {
        ARCS_DIR / subj / topic / f"{lang}.json"
        for subj, topic, lang in REEXPORT
    }

    files        = sorted(ARCS_DIR.rglob("*.json"))
    total_fixed  = 0
    files_touched = 0

    for f in files:
        if f in reexport_paths:
            continue   # will be re-exported in pass 2 — skip

        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  SKIP {f.name}: parse error - {e}")
            continue

        label    = "/".join(f.parts[-3:])

        # Rescue problems from non-standard top-level keys (runs even if problems=[])
        n_rescue = fix_misplaced_problems(data, label)

        problems = data.get("problems", [])
        if not problems:
            if n_rescue == 0:
                print(f"  [{label}] ok - no fixes needed")
            continue

        n0 = fix_wrong_field_names(problems, label)  # must run first
        n1 = fix_correct_index(problems, label)
        n2 = fix_hints_array(problems, label)
        n3 = fix_invalid_stages(problems, label)
        total = n_rescue + n0 + n1 + n2 + n3

        if total > 0:
            files_touched += 1
            print(f"  [{label}] {total} fix(es) applied")
            if not dry_run:
                f.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8"
                )
        elif n_rescue == 0:
            print(f"  [{label}] ok - no fixes needed")

        total_fixed += total

    print(f"\n  Pass 1 complete: {total_fixed} fix(es) across {files_touched} file(s)")
    return total_fixed


def pass2_reexport(provider: str, model: str, dry_run: bool = False):
    """
    Delete broken JSON files and re-run export_json.py for each one.
    """
    print(f"\n  Pass 2 - re-exporting {len(REEXPORT)} file(s) with {provider}/{model}")

    for subj, topic, lang in REEXPORT:
        json_path = ARCS_DIR / subj / topic / f"{lang}.json"
        md_path   = ARCS_DIR / subj / topic / f"{lang}.md"
        label     = f"{subj}/{topic}/{lang}"

        if not md_path.exists():
            print(f"  ERROR [{label}]: .md source file not found - cannot re-export")
            continue

        # Delete broken JSON so export_json.py will process it
        if json_path.exists():
            if not dry_run:
                json_path.unlink()
            print(f"  Deleted: {json_path.name}  ({label})")

        if dry_run:
            print(f"  Would re-export: {label}")
            continue

        print(f"  Re-exporting: {label} ...", end="", flush=True)
        result = subprocess.run(
            [
                sys.executable, str(CONTENT_DIR / "export_json.py"),
                "--grade",   "7",
                "--subject", subj,
                "--topic",   topic,
                "--language", lang,
                "--provider", provider,
                "--model",    model,
            ],
            capture_output=True, text=True, cwd=str(CONTENT_DIR)
        )
        if json_path.exists():
            print(" done")
        else:
            print(" FAILED")
            if result.stderr:
                # Print first relevant error line
                for line in result.stderr.splitlines():
                    if "ERROR" in line or "Error" in line or "Traceback" in line:
                        print(f"    {line.strip()}")
                        break


def main():
    parser = argparse.ArgumentParser(description="Fix Grade 7 arc JSON files.")
    parser.add_argument("--provider",      default="ollama",
                        choices=["anthropic", "gemini", "groq", "ollama"])
    parser.add_argument("--model",         default=None,
                        help="Model override (default: provider's recommended model)")
    parser.add_argument("--fix-only",      action="store_true",
                        help="Run Pass 1 only — no re-exports")
    parser.add_argument("--reexport-only", action="store_true",
                        help="Run Pass 2 only — skip in-place fixes")
    parser.add_argument("--dry-run",       action="store_true",
                        help="Show what would be done, no writes")
    args = parser.parse_args()

    MODEL_DEFAULTS = {
        "anthropic": "claude-opus-4-5",
        "gemini":    "gemini-2.0-flash",
        "groq":      "llama-3.3-70b-versatile",
        "ollama":    "qwen3-coder:30b",
    }
    model = args.model or MODEL_DEFAULTS[args.provider]

    print(f"\n  fix_grade7_arcs.py")
    print(f"  Provider: {args.provider} | Model: {model}")
    if args.dry_run:
        print("  DRY RUN - no files will be written\n")

    if not args.reexport_only:
        print("\n== PASS 1: In-place fixes ==")
        pass1_auto_fix(dry_run=args.dry_run)

    if not args.fix_only:
        print("\n== PASS 2: Re-export broken files ==")
        pass2_reexport(provider=args.provider, model=model, dry_run=args.dry_run)

    if not args.dry_run:
        print("\n  All done. Run validate_arcs.py --grade 7 to confirm.")


if __name__ == "__main__":
    main()
