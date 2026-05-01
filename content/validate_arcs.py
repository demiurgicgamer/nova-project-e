#!/usr/bin/env python3
"""
validate_arcs.py - quick schema + content check for generated arc JSON files.
Usage: python validate_arcs.py --grade 7
"""

import argparse
import json
from pathlib import Path

CONTENT_DIR = Path(__file__).parent
ARCS_DIR    = CONTENT_DIR / "arcs"

REQUIRED_STAGES      = {"guided", "practice", "capstone"}
VALID_DIFFICULTIES   = {"easy", "medium", "medium_hard", "hard", "capstone"}

GRADE_TOPICS = {
    7: {
        "mathematics": [
            "proportional_reasoning", "rational_numbers", "two_step_equations",
            "geometry_circles", "surface_area_and_volume", "statistics_and_sampling",
            "probability_compound",
        ],
        "physics":   ["heat_and_thermal_energy", "the_solar_system", "energy_and_work"],
        "chemistry": ["atoms_and_elements", "acids_and_bases"],
        "biology":   ["diversity_of_living_things", "cell_division_and_growth", "heredity_and_traits"],
    },
    6: {
        "mathematics": [
            "fractions", "integers", "ratios", "percentages", "algebra_basics",
            "linear_equations", "geometry_area_perimeter", "probability", "data_and_graphs",
        ],
        "physics":   ["forces_and_motion", "simple_machines", "electricity_basics", "light_and_optics"],
        "chemistry": ["matter_and_materials", "mixtures_and_solutions", "physical_vs_chemical_changes"],
        "biology":   ["cells_and_life", "ecosystems", "human_body_systems"],
    },
}


def validate(grade: int):
    topics    = GRADE_TOPICS.get(grade, {})
    arcs_base = ARCS_DIR / f"grade_{grade}"
    languages = ["en", "fr"]

    errors   = []
    warnings = []
    stats    = {}

    # ── Check for missing files ───────────────────────────────────────────────
    missing = []
    for subj, tlist in topics.items():
        for t in tlist:
            for lang in languages:
                p = arcs_base / subj / t / f"{lang}.json"
                if not p.exists():
                    missing.append(f"{subj}/{t}/{lang}.json")

    total_expected = sum(len(v) for v in topics.values()) * len(languages)
    files          = sorted(arcs_base.rglob("*.json"))

    print(f"\nGrade {grade} - {len(files)}/{total_expected} JSON files found")
    if missing:
        print("\n  MISSING FILES:")
        for m in missing:
            print(f"    MISSING: {m}")

    # ── Validate each file ────────────────────────────────────────────────────
    print()
    for f in files:
        label = "/".join(f.parts[-3:])

        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"{label}: JSON parse error - {e}")
            continue

        meta     = data.get("meta", {})
        hooks    = data.get("hook_stories", [])
        concept  = data.get("concept", {})
        problems = data.get("problems", [])

        # ── Meta ─────────────────────────────────────────────────────────────
        for field in ["subject", "topic_key", "topic_display", "grade", "language_code"]:
            if not meta.get(field):
                errors.append(f"{label}: meta.{field} missing or empty")

        # ── Hook stories ─────────────────────────────────────────────────────
        if len(hooks) < 2:
            warnings.append(f"{label}: only {len(hooks)} hook story (need >=2)")
        for i, h in enumerate(hooks):
            if not (h.get("text") or "").strip():
                errors.append(f"{label}: hook_stories[{i}] has no text")
            if not (h.get("closing_line") or "").strip():
                warnings.append(f"{label}: hook_stories[{i}] missing closing_line")

        # ── Concept ───────────────────────────────────────────────────────────
        if not (concept.get("comprehension_check") or "").strip():
            errors.append(f"{label}: concept.comprehension_check empty")
        if not concept.get("explanation_steps"):
            errors.append(f"{label}: concept.explanation_steps empty")
        if len(concept.get("reteach_angles", [])) < 2:
            warnings.append(f"{label}: concept.reteach_angles < 2")

        # ── Problems ──────────────────────────────────────────────────────────
        if not problems:
            errors.append(f"{label}: no problems at all")
            stats[label] = {"hooks": len(hooks), "problems": 0, "stages": {}}
            continue

        stage_counts = {}
        for p in problems:
            pid   = p.get("id", "?")
            stage = p.get("stage", "")
            diff  = str(p.get("difficulty", ""))
            mc    = p.get("mc_choices") or []
            cidx  = p.get("correct_index", -1)
            hints = p.get("intervention_hints")
            text  = (p.get("text") or p.get("question") or "").strip()

            stage_counts[stage] = stage_counts.get(stage, 0) + 1

            if stage not in REQUIRED_STAGES:
                errors.append(f"{label}: [{pid}] invalid stage='{stage}'")
            if diff.lower() not in VALID_DIFFICULTIES:
                warnings.append(f"{label}: [{pid}] unusual difficulty='{diff}'")
            if len(mc) != 4:
                errors.append(f"{label}: [{pid}] has {len(mc)} mc_choices (need 4)")
            if int(cidx) != 0:
                errors.append(f"{label}: [{pid}] correct_index={cidx} (must be 0)")
            if not text:
                errors.append(f"{label}: [{pid}] missing problem text/question field")
            if not (p.get("answer_explanation") or "").strip():
                warnings.append(f"{label}: [{pid}] missing answer_explanation")

            # intervention_hints: accept dict {level_1/2/3} or list [h1,h2,h3]
            if isinstance(hints, list):
                if len(hints) < 3:
                    errors.append(f"{label}: [{pid}] intervention_hints list has only {len(hints)} item(s)")
                else:
                    warnings.append(f"{label}: [{pid}] intervention_hints is array not dict - needs fix_hints pass")
            elif isinstance(hints, dict):
                for lvl in ("level_1", "level_2", "level_3"):
                    if not (hints.get(lvl) or "").strip():
                        errors.append(f"{label}: [{pid}] intervention_hints.{lvl} empty")
            else:
                errors.append(f"{label}: [{pid}] intervention_hints missing or wrong type")

        # Check all three arc stages are represented
        for req_stage in REQUIRED_STAGES:
            if req_stage not in stage_counts:
                warnings.append(f"{label}: no '{req_stage}' stage problems found")

        stats[label] = {"hooks": len(hooks), "problems": len(problems), "stages": stage_counts}

    # ── Report ────────────────────────────────────────────────────────────────
    print("=" * 64)
    print("  ERRORS")
    print("=" * 64)
    if errors:
        for e in errors:
            print(f"  ERR  {e}")
    else:
        print("  None - all files pass schema checks!")

    print()
    print("=" * 64)
    print("  WARNINGS")
    print("=" * 64)
    if warnings:
        for w in warnings:
            print(f"  WARN {w}")
    else:
        print("  None!")

    print()
    print("=" * 64)
    print("  FILE STATS")
    print("=" * 64)
    total_p = total_h = 0
    for label, s in stats.items():
        total_p += s["problems"]
        total_h += s["hooks"]
        stage_str = " ".join(f"{k}:{v}" for k, v in sorted(s["stages"].items()))
        print(f"  {label:<58} h={s['hooks']} p={s['problems']:2d}  [{stage_str}]")

    print(f"\n  TOTAL: {len(files)} files | {total_h} hook stories | {total_p} problems")
    print(f"  Errors: {len(errors)}  |  Warnings: {len(warnings)}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--grade", required=True, type=int)
    args = parser.parse_args()
    validate(args.grade)
