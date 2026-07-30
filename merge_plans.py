"""
merge_plans.py — Merges run_plan.json, strength_plan.json, and nutrition_plan.json
into a unified daily-keyed JSON file.

Usage:
    python merge_plans.py \
        --run run_plan.json \
        --strength strength_plan.json \
        --nutrition nutrition_plan.json \
        --output-full combined_plan.json \
        --output-week1 week1_plan.json

Assumptions about source files (validated on load):
  - run / strength: top-level keys include "meta", "weeks" (array of week objects),
    "session_protocols", "daily_routines"
  - nutrition: top-level keys include "meta", "day_types" (array), "supplements"
  - All dates are ISO 8601 YYYY-MM-DD strings
"""

import json
import argparse
from pathlib import Path
from collections import defaultdict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_week_range(s: str) -> tuple[int, int]:
    """Parse "1-13" → (1, 13). Also handles bare "7" → (7, 7)."""
    parts = s.split("-")
    if len(parts) == 2:
        return int(parts[0]), int(parts[1])
    return int(parts[0]), int(parts[0])


def weeks_to_days(weeks: list[dict], plan_type: str) -> dict[str, dict]:
    """
    Flatten a weeks array into a dict keyed by date string.
    Returns { "2026-04-27": { day fields … }, … }
    Attaches week_number, phase, week_notes, day_notes to each day.
    """
    result = {}
    for week in weeks:
        wnum = week.get("week_number")
        phase = week.get("phase")
        week_notes = week.get("week_notes", "")
        weekly_targets = week.get("weekly_targets", {})
        for day in week.get("days", []):
            date = day["date"]
            result[date] = {
                "date": date,
                "day_of_week": day.get("day_of_week"),
                "week_number": wnum,
                "phase": phase,
                "weekly_targets": weekly_targets,
                "week_notes": week_notes,
                "day_notes": day.get("day_notes", ""),
                "flex": day.get("flex", False),
                "rest_day": day.get("rest_day", False),
                "sessions": day.get("sessions", []),
            }
    return result


def resolve_nutrition(date: str, run_day: dict | None, strength_day: dict | None,
                      day_types: list[dict]) -> dict | None:
    """
    Match a day to a nutrition day_type template.
    Returns the resolved nutrition block or None.
    """
    # Extract subtypes
    run_subtype = None
    strength_subtype = None

    if run_day:
        sessions = run_day.get("sessions", [])
        run_sessions = [s for s in sessions if s.get("type") == "run"]
        if run_sessions:
            run_subtype = run_sessions[0].get("subtype")

    if strength_day:
        sessions = strength_day.get("sessions", [])
        str_sessions = [s for s in sessions if s.get("type") == "strength"]
        if str_sessions:
            strength_subtype = str_sessions[0].get("subtype")

    # Determine the phase for adjustment lookup — prefer run phase, fall back to strength
    phase = None
    if run_day:
        phase = run_day.get("phase")
    elif strength_day:
        phase = strength_day.get("phase")

    # Find matching day_type
    matched = None
    for dt in day_types:
        for trigger in dt.get("triggers", []):
            if trigger.get("run_subtype") == run_subtype and \
               trigger.get("strength_subtype") == strength_subtype:
                matched = dt
                break
        if matched:
            break

    # Fallback to rest_day if nothing matched
    if not matched:
        for dt in day_types:
            if dt.get("id") == "rest_day":
                matched = dt
                break

    if not matched:
        return None

    # Resolve phase adjustment
    phase_adjustment_applied = None
    daily_targets = dict(matched.get("daily_targets", {}))

    for adj in matched.get("phase_adjustments", []):
        if adj.get("phase") == phase:
            phase_adjustment_applied = adj
            # Override base targets with adjusted values
            daily_targets = dict(daily_targets)
            daily_targets.update(adj.get("modified_targets", {}))
            break

    return {
        "day_type_id": matched.get("id"),
        "day_type_label": matched.get("label"),
        "phase_adjustment_applied": phase_adjustment_applied,
        "daily_targets": daily_targets,
        "session_fueling": matched.get("session_fueling", []),
        "notes": matched.get("notes", ""),
    }


# ---------------------------------------------------------------------------
# Main merge logic
# ---------------------------------------------------------------------------

def merge(run_path: Path, strength_path: Path, nutrition_path: Path) -> dict:
    run_data = json.loads(run_path.read_text())
    strength_data = json.loads(strength_path.read_text())
    nutrition_data = json.loads(nutrition_path.read_text())

    # --- Build unified meta ---
    run_meta = run_data["meta"]
    str_meta = strength_data["meta"]
    nut_meta = nutrition_data["meta"]

    # plan_end: take the latest across all three files
    plan_end = max(
        run_meta.get("plan_end", ""),
        str_meta.get("plan_end", ""),
        nut_meta.get("plan_end", ""),
    )

    meta = {
        "athlete": run_meta.get("athlete"),
        "race": run_meta.get("race"),
        "race_date": run_meta.get("race_date"),
        "plan_start": run_meta.get("plan_start"),
        "plan_end": plan_end,
        "sources": {
            "run": {
                "schema_version": run_meta.get("schema_version"),
                "generated_by": run_meta.get("generated_by"),
                "generated_date": run_meta.get("generated_date"),
            },
            "strength": {
                "schema_version": str_meta.get("schema_version"),
                "generated_by": str_meta.get("generated_by"),
                "generated_date": str_meta.get("generated_date"),
            },
            "nutrition": {
                "schema_version": nut_meta.get("schema_version"),
                "generated_by": nut_meta.get("generated_by"),
                "generated_date": nut_meta.get("generated_date"),
            },
        },
    }

    # --- Session protocols (namespaced) ---
    session_protocols = {
        "run": run_data.get("session_protocols", {}),
        "strength": strength_data.get("session_protocols", {}),
    }

    # --- Daily routines (owned by strength) ---
    daily_routines = strength_data.get("daily_routines", [])

    # --- Supplements (owned by nutrition) ---
    supplements = nutrition_data.get("supplements", [])

    # --- Flatten weeks → day dicts ---
    run_days = weeks_to_days(run_data.get("weeks", []), "run")
    strength_days = weeks_to_days(strength_data.get("weeks", []), "strength")
    day_types = nutrition_data.get("day_types", [])

    # --- Collect all dates ---
    all_dates = sorted(set(run_days.keys()) | set(strength_days.keys()))

    # --- Build merged days ---
    days = {}
    for date in all_dates:
        run_day = run_days.get(date)
        str_day = strength_days.get(date)

        # Shared calendar fields — run plan is primary source
        source = run_day or str_day
        day_of_week = source.get("day_of_week")
        week_number = source.get("week_number")
        flex = (run_day or {}).get("flex", (str_day or {}).get("flex", False))

        # rest_day: true only if both sides agree there's nothing to do
        run_is_rest = not run_day or (run_day.get("rest_day", False) or not run_day.get("sessions"))
        str_is_rest = not str_day or (str_day.get("rest_day", False) or not str_day.get("sessions"))
        rest_day = run_is_rest and str_is_rest

        # Build run namespace
        if run_day:
            run_block = {
                "phase": run_day.get("phase"),
                "weekly_targets": run_day.get("weekly_targets"),
                "week_notes": run_day.get("week_notes", ""),
                "day_notes": run_day.get("day_notes", ""),
                "sessions": run_day.get("sessions", []),
            }
        else:
            run_block = None

        # Build strength namespace
        if str_day:
            strength_block = {
                "phase": str_day.get("phase"),
                "weekly_targets": str_day.get("weekly_targets"),
                "week_notes": str_day.get("week_notes", ""),
                "day_notes": str_day.get("day_notes", ""),
                "sessions": str_day.get("sessions", []),
            }
        else:
            strength_block = None

        # Resolve nutrition
        nutrition_block = resolve_nutrition(date, run_day, str_day, day_types)

        days[date] = {
            "date": date,
            "day_of_week": day_of_week,
            "week_number": week_number,
            "flex": flex,
            "rest_day": rest_day,
            "run": run_block,
            "strength": strength_block,
            "nutrition": nutrition_block,
        }

    return {
        "meta": meta,
        "session_protocols": session_protocols,
        "daily_routines": daily_routines,
        "supplements": supplements,
        "days": days,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Merge 50K training plan JSON files")
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--strength", required=True, type=Path)
    parser.add_argument("--nutrition", required=True, type=Path)
    parser.add_argument("--output-full", required=True, type=Path)
    parser.add_argument("--output-week1", required=True, type=Path)
    args = parser.parse_args()

    print("Merging plans…")
    combined = merge(args.run, args.strength, args.nutrition)

    # Write full output
    args.output_full.write_text(json.dumps(combined, indent=2, ensure_ascii=False))
    total_days = len(combined["days"])
    print(f"  Full plan: {total_days} days → {args.output_full}")

    # Build week-1 slice
    week1_days = {
        date: day for date, day in combined["days"].items()
        if day.get("week_number") == 1
    }
    week1 = dict(combined)  # shallow copy of top-level
    week1["days"] = week1_days
    week1["meta"] = dict(combined["meta"])
    week1["meta"]["slice"] = "week_1_only"

    args.output_week1.write_text(json.dumps(week1, indent=2, ensure_ascii=False))
    print(f"  Week 1 slice: {len(week1_days)} days → {args.output_week1}")

    # Print a quick summary table
    print("\nDate coverage summary:")
    dates = sorted(combined["days"].keys())
    print(f"  First date : {dates[0]}")
    print(f"  Last date  : {dates[-1]}")
    print(f"  Total days : {len(dates)}")

    # Validate: check every day has a nutrition block
    missing_nutrition = [d for d, v in combined["days"].items() if v["nutrition"] is None]
    if missing_nutrition:
        print(f"\n  WARNING: {len(missing_nutrition)} days have no matched nutrition template:")
        for d in missing_nutrition:
            print(f"    {d}")
    else:
        print("  Nutrition matched: all days ✓")

    # Summarise nutrition type distribution
    type_counts = defaultdict(int)
    for day in combined["days"].values():
        nut = day.get("nutrition")
        if nut:
            type_counts[nut["day_type_id"]] += 1
    print("\nNutrition day-type distribution:")
    for tid, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {tid:40s} {count}")

    print("\nDone.")


if __name__ == "__main__":
    main()
