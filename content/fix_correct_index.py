#!/usr/bin/env python3
"""
Nova Arc Content — fix_correct_index.py
========================================
Scans all exported arc JSON files and repairs the `correct_index` field.

The arc generator always set correct_index = 0, but the correct answer is
not always the first element of mc_choices.  This script:
  1. Reads every arc JSON under content/arcs/
  2. For each problem that has both `answer` and `mc_choices`, resolves
     the true index by matching the answer string to the choices list.
  3. Optionally shuffles mc_choices so the correct answer is not always
     at the same position (--shuffle).
  4. Writes the repaired JSON in-place (dry-run by default).

Matching strategy (tried in order):
  a) Exact string match of `answer` against a choice.
  b) Letter-prefix match — if `answer` is "B" or starts with "B)" / "B.",
     find the choice whose text starts with "B)" or "B.".
  c) Substring match — `answer` is contained in a choice (handles cases
     like answer="3/10" inside choice="C) 3/10").
  d) If nothing matches → flag as UNRESOLVED and skip (do not corrupt file).

Usage:
  python fix_correct_index.py                   # dry run — report only
  python fix_correct_index.py --write           # write repaired files in-place
  python fix_correct_index.py --write --shuffle # also randomise choice order
  python fix_correct_index.py --grade 6 --subject mathematics --write
  python fix_correct_index.py --file path/to/en.json --write
"""

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
CONTENT_DIR = Path(__file__).parent
ARCS_DIR    = CONTENT_DIR / "arcs"

# ── Matching helpers ──────────────────────────────────────────────────────────

def _normalise(s: str) -> str:
    """Lower-case, strip whitespace."""
    return s.strip().lower()

def _letter_from_answer(answer: str) -> str | None:
    """
    Extract the leading letter from answers like 'B', 'B)', 'B.', 'B) text...'.
    Returns upper-case single letter, or None.
    """
    m = re.match(r"^([A-Da-d])\s*[).\s]", answer.strip())
    if m:
        return m.group(1).upper()
    # bare single letter
    if re.match(r"^[A-Da-d]$", answer.strip()):
        return answer.strip().upper()
    return None

def _choice_letter(choice: str) -> str | None:
    """Extract leading letter from a choice like 'B) text' or 'B. text'."""
    m = re.match(r"^([A-Da-d])\s*[).]\s*", choice.strip())
    return m.group(1).upper() if m else None

def resolve_correct_index(answer: str, mc_choices: list[str]) -> int | None:
    """
    Return the 0-based index of the correct choice, or None if unresolvable.
    """
    # a) exact match
    for i, choice in enumerate(mc_choices):
        if _normalise(choice) == _normalise(answer):
            return i

    # b) letter-prefix match
    answer_letter = _letter_from_answer(answer)
    if answer_letter:
        for i, choice in enumerate(mc_choices):
            if _choice_letter(choice) == answer_letter:
                return i

    # c) substring match (answer contained inside choice, or vice-versa)
    ans_norm = _normalise(answer)
    for i, choice in enumerate(mc_choices):
        choice_norm = _normalise(choice)
        if ans_norm in choice_norm or choice_norm in ans_norm:
            return i

    # d) last-resort: bare letter used as ordinal (A=0, B=1, C=2, D=3)
    #    Only applies when none of the choices have letter prefixes, so the
    #    agent used A/B/C/D to mean "first/second/third/fourth option".
    #    If any choice already starts with a letter prefix we skip this to
    #    avoid colliding with pattern (b).
    none_have_prefix = all(_choice_letter(c) is None for c in mc_choices)
    letter_ord = {"A": 0, "B": 1, "C": 2, "D": 3}
    bare_letter = answer.strip().upper()
    if none_have_prefix and bare_letter in letter_ord:
        idx = letter_ord[bare_letter]
        if idx < len(mc_choices):
            return idx

    return None  # unresolved

# ── File processing ───────────────────────────────────────────────────────────

def process_file(path: Path, write: bool, shuffle: bool) -> dict:
    """
    Inspect one JSON file.  Returns a result dict with stats.
    """
    result = {
        "file":      str(path.relative_to(CONTENT_DIR)),
        "problems":  0,
        "fixed":     0,
        "already_ok":0,
        "unresolved":[],
        "shuffled":  0,
    }

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    problems = data.get("problems", [])
    # also handle old-format stages array (fractions/en.json reference arc)
    if not problems:
        for stage in data.get("stages", []):
            for p in stage.get("problems", []):
                problems.append(p)

    changed = False

    for prob in problems:
        mc = prob.get("mc_choices")
        answer = prob.get("answer")
        if not mc or answer is None:
            continue

        result["problems"] += 1
        pid = prob.get("id") or prob.get("problem_id") or "?"

        idx = resolve_correct_index(str(answer), mc)

        if idx is None:
            result["unresolved"].append({
                "id":     pid,
                "answer": answer,
                "choices":mc,
            })
            continue

        # Shuffle choices (before updating correct_index so we track new index)
        if shuffle:
            # build (choice, is_correct) pairs, shuffle, unpack
            annotated = [(c, i == idx) for i, c in enumerate(mc)]
            random.shuffle(annotated)
            new_choices   = [c for c, _ in annotated]
            new_idx       = next(i for i, (_, correct) in enumerate(annotated) if correct)
            if new_choices != mc:
                prob["mc_choices"]    = new_choices
                prob["correct_index"] = new_idx
                result["shuffled"] += 1
                changed = True
                if prob.get("correct_index") != new_idx or mc != new_choices:
                    result["fixed"] += 1
                else:
                    result["already_ok"] += 1
                continue  # skip the non-shuffle branch below

        # No shuffle — just fix the index
        current = prob.get("correct_index")
        if current == idx:
            result["already_ok"] += 1
        else:
            prob["correct_index"] = idx
            result["fixed"] += 1
            changed = True

    if write and changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        result["written"] = True
    else:
        result["written"] = False

    return result

# ── File discovery ────────────────────────────────────────────────────────────

def collect_files(grade: int | None, subject: str | None, topic: str | None,
                  language: str | None, single_file: Path | None) -> list[Path]:
    if single_file:
        return [single_file]

    base = ARCS_DIR
    if grade:
        base = base / f"grade_{grade}"
    if subject:
        base = base / subject
    if topic:
        base = base / topic

    files = sorted(base.rglob("*.json"))

    if language:
        files = [f for f in files if f.stem == language]

    return files

# ── Reporting ─────────────────────────────────────────────────────────────────

def print_report(results: list[dict], write: bool, shuffle: bool):
    total_problems  = sum(r["problems"]   for r in results)
    total_fixed     = sum(r["fixed"]      for r in results)
    total_ok        = sum(r["already_ok"] for r in results)
    total_shuffled  = sum(r["shuffled"]   for r in results)
    total_unresolved= sum(len(r["unresolved"]) for r in results)

    mode = "WRITE" if write else "DRY RUN"
    print(f"\n{'='*60}")
    print(f"  fix_correct_index — {mode}")
    print(f"{'='*60}")
    print(f"  Files scanned  : {len(results)}")
    print(f"  Problems found : {total_problems}")
    print(f"  Already correct: {total_ok}")
    print(f"  Fixed          : {total_fixed}")
    if shuffle:
        print(f"  Shuffled       : {total_shuffled}")
    print(f"  Unresolved     : {total_unresolved}")
    print()

    # Per-file summary
    for r in results:
        status_parts = []
        if r["fixed"]:
            status_parts.append(f"FIXED {r['fixed']}")
        if r["shuffled"] and shuffle:
            status_parts.append(f"shuffled {r['shuffled']}")
        if r["unresolved"]:
            status_parts.append(f"WARN {len(r['unresolved'])} unresolved")
        if not status_parts:
            status_parts.append("ok")
        written = " [written]" if r.get("written") else ""
        print(f"  {r['file']:<55} {', '.join(status_parts)}{written}")

    # Unresolved detail
    any_unresolved = [r for r in results if r["unresolved"]]
    if any_unresolved:
        print(f"\n{'-'*60}")
        print("  UNRESOLVED - manual fix required:")
        print(f"{'-'*60}")
        for r in any_unresolved:
            print(f"\n  >> {r['file']}")
            for u in r["unresolved"]:
                print(f"     Problem : {u['id']}")
                print(f"     Answer  : {u['answer']}")
                print(f"     Choices :")
                for i, c in enumerate(u["choices"]):
                    print(f"       [{i}] {c}")

    if not write:
        print(f"\n  [DRY RUN] No files written. Re-run with --write to apply fixes.")
    else:
        written_count = sum(1 for r in results if r.get("written"))
        print(f"\n  [DONE] {written_count} file(s) written.")

    print()

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Repair correct_index in arc JSON files."
    )
    parser.add_argument("--grade",    type=int,    help="Filter by grade number (e.g. 6)")
    parser.add_argument("--subject",  type=str,    help="Filter by subject folder (e.g. mathematics)")
    parser.add_argument("--topic",    type=str,    help="Filter by topic folder (e.g. fractions)")
    parser.add_argument("--language", type=str,    help="Filter by language (en or fr)")
    parser.add_argument("--file",     type=Path,   help="Process a single JSON file")
    parser.add_argument("--write",    action="store_true",
                        help="Write repaired files in-place (default: dry run)")
    parser.add_argument("--shuffle",  action="store_true",
                        help="Also randomise mc_choices order (distributes correct answer across A/B/C/D)")
    parser.add_argument("--seed",     type=int,    default=None,
                        help="Random seed for --shuffle (for reproducible output)")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    files = collect_files(
        grade       = args.grade,
        subject     = args.subject,
        topic       = args.topic,
        language    = args.language,
        single_file = args.file,
    )

    if not files:
        print("No JSON files found matching the given filters.")
        sys.exit(1)

    results = []
    for path in files:
        try:
            r = process_file(path, write=args.write, shuffle=args.shuffle)
            results.append(r)
        except Exception as e:
            print(f"ERROR processing {path}: {e}", file=sys.stderr)

    print_report(results, write=args.write, shuffle=args.shuffle)

    # Exit 1 if any unresolved (useful in CI)
    if any(r["unresolved"] for r in results):
        sys.exit(1)

if __name__ == "__main__":
    main()
