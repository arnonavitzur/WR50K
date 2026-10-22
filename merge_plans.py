"""
merge_plans.py — Merges run_plan.json, strength_plan.json and nutrition_plan.json
into a unified, date-keyed combined_plan.json.

Usage:
    python merge_plans.py \
        --run run_plan.json \
        --strength strength_plan.json \
        --nutrition nutrition_plan.json \
        --output-full combined_plan.json \
        [--output-week1 week1_plan.json] [--slice-week 1] \
        [--schema master_schema_v7.json]

Schema support: master schema v1.2 - v1.8 (additive changes only, so older
source files still merge).

What changed for schema 1.7 / 1.8 (vs the May 2026 script):
  - Cross sessions (type "cross", e.g. swims) are recognised wherever they appear
    (the strength plan carries them). They make a day non-rest, feed nutrition
    matching, and never enter run sums.
  - Nutrition triggers may carry cross_subtype: present -> matches only when a
    cross session of that subtype is scheduled (null -> only when none is);
    absent -> cross sessions are ignored. First match wins, as before.
  - Nutrition date_overrides are applied (exact date match replaces the day
    type's targets/fueling). The previous script ignored them.
  - Run sessions: elevation_loss_ft is filled in when absent (= elevation_gain_ft,
    the schema default) so the app never has to apply the rule itself.
  - aet_gate: when active_version == "if_pass", the if_pass fields are copied over
    the session's top-level fields (the schema's promotion rule). The aet_gate
    object is kept for reference.
  - meta gains the run plan's HR anchors (LTHR / AeT fields) and each source's
    revision. weekly_targets pass through untouched, so descent_ft, ua_week_type,
    run_hours_est, recovery_week, swim_sessions and swim_minutes all reach the app.
  - The nutrition templates (day_types + date_overrides) are stored in the
    combined file under "nutrition_source", so update_plan.py can re-resolve
    nutrition after a run or strength update.
  - Derived weekly targets are checked against the session sums and reported.

All changes to the output are additive: existing fields keep their names and
shapes, so the dashboard keeps working.
"""

import argparse
import copy
import json
from collections import defaultdict
from datetime import date as _date
from pathlib import Path

KNOWN_SCHEMA_VERSIONS = {"1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8"}

# Run-plan meta fields carried into the combined meta (schema 1.6+).
HR_META_FIELDS = [
    "lthr_bpm", "lthr_updated", "lthr_confidence", "lthr_source",
    "aet_bpm", "aet_status", "aet_gate_bpm", "aet_test_dates",
]

# Tolerances from the run plan's derivation_rule.
TOL_MILES = 0.5
TOL_FEET = 50
TOL_HOURS = 0.1


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_plan(path: Path, expected_type: str) -> dict:
    data = json.loads(Path(path).read_text())
    meta = data.get("meta", {})
    ptype = meta.get("plan_type")
    if ptype and ptype != expected_type:
        raise SystemExit(f"{path}: meta.plan_type is '{ptype}', expected '{expected_type}'")
    required = (["meta", "day_types", "supplements"] if expected_type == "nutrition"
                else ["meta", "weeks"])
    missing = [k for k in required if k not in data]
    if missing:
        raise SystemExit(f"{path}: missing top-level key(s): {', '.join(missing)}")
    sv = meta.get("schema_version")
    if sv not in KNOWN_SCHEMA_VERSIONS:
        print(f"  WARNING: {path} has schema_version {sv!r}; this script knows "
              f"{', '.join(sorted(KNOWN_SCHEMA_VERSIONS))}")
    return data


def validate_against_schema(schema_path: Path, plans: dict[str, dict]) -> None:
    """Optional full JSON Schema validation (needs `pip install jsonschema`)."""
    try:
        import jsonschema
    except ImportError:
        print("  NOTE: --schema given but jsonschema is not installed "
              "(pip install jsonschema); skipping schema validation.")
        return
    schema = json.loads(Path(schema_path).read_text())
    validator = jsonschema.Draft7Validator(schema)
    for name, data in plans.items():
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        if errors:
            print(f"  SCHEMA: {name} has {len(errors)} error(s); first 10:")
            for e in errors[:10]:
                loc = "/".join(str(p) for p in e.path) or "(root)"
                print(f"    {loc}: {e.message[:160]}")
        else:
            print(f"  SCHEMA: {name} valid ✓")


# ---------------------------------------------------------------------------
# Session normalisation
# ---------------------------------------------------------------------------

def normalize_session(session: dict) -> dict:
    """Return a copy with schema defaults made explicit."""
    s = copy.deepcopy(session)
    if s.get("type") == "run":
        # v1.8: absent elevation_loss_ft = equal to elevation_gain_ft
        if "elevation_loss_ft" not in s and "elevation_gain_ft" in s:
            s["elevation_loss_ft"] = s["elevation_gain_ft"]
        # v1.6: aet_gate promotion copies if_pass over the top-level fields
        gate = s.get("aet_gate")
        if gate and gate.get("active_version") == "if_pass":
            for k, v in (gate.get("if_pass") or {}).items():
                s[k] = v
            s["aet_gate_applied"] = "if_pass"
    return s


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------

def weeks_to_days(weeks: list[dict], plan_type: str = "") -> dict[str, dict]:
    """
    Flatten a weeks array into { "YYYY-MM-DD": {day fields...} }.
    Attaches week_number, phase, weekly_targets and week_notes to each day.
    (plan_type is accepted for backward compatibility and unused.)
    """
    result = {}
    for week in weeks:
        for day in week.get("days", []):
            d = day["date"]
            result[d] = {
                "date": d,
                "day_of_week": day.get("day_of_week"),
                "week_number": week.get("week_number"),
                "phase": week.get("phase"),
                "weekly_targets": week.get("weekly_targets", {}),
                "week_notes": week.get("week_notes", ""),
                "day_notes": day.get("day_notes", ""),
                "flex": day.get("flex", False),
                "rest_day": day.get("rest_day", False),
                "sessions": [normalize_session(s) for s in day.get("sessions", [])],
            }
    return result


def build_block(day: dict | None) -> dict | None:
    """The per-plan namespace stored under days[date].run / .strength."""
    if not day:
        return None
    return {
        "phase": day.get("phase"),
        "weekly_targets": day.get("weekly_targets"),
        "week_notes": day.get("week_notes", ""),
        "day_notes": day.get("day_notes", ""),
        "sessions": day.get("sessions", []),
    }


# Kept for callers of the old names.
build_run_block = build_block
build_strength_block = build_block


def compute_rest_day(run_block: dict | None, strength_block: dict | None) -> bool:
    """A day is rest only if neither plan schedules any session (run, strength or cross)."""
    return not (run_block or {}).get("sessions") and not (strength_block or {}).get("sessions")


# ---------------------------------------------------------------------------
# Nutrition resolution
# ---------------------------------------------------------------------------

def _sessions(*blocks: dict | None) -> list[dict]:
    out = []
    for b in blocks:
        if b:
            out.extend(b.get("sessions", []))
    return out


def day_signature(run_block: dict | None, strength_block: dict | None) -> dict:
    """Subtypes the nutrition triggers match on."""
    sessions = _sessions(run_block, strength_block)
    run = [s for s in (run_block or {}).get("sessions", []) if s.get("type") == "run"]
    strength = [s for s in (strength_block or {}).get("sessions", []) if s.get("type") == "strength"]
    cross = [s for s in sessions if s.get("type") == "cross"]
    return {
        "run_subtype": run[0].get("subtype") if run else None,
        "strength_subtype": strength[0].get("subtype") if strength else None,
        "cross_subtypes": [s.get("subtype") for s in cross],
    }


def trigger_matches(trigger: dict, sig: dict) -> bool:
    if trigger.get("run_subtype") != sig["run_subtype"]:
        return False
    if trigger.get("strength_subtype") != sig["strength_subtype"]:
        return False
    if "cross_subtype" in trigger:
        want = trigger["cross_subtype"]
        if want is None:
            return not sig["cross_subtypes"]
        return want in sig["cross_subtypes"]
    return True  # no cross_subtype key -> cross sessions ignored


def match_day_type(sig: dict, day_types: list[dict]) -> tuple[dict | None, str]:
    for dt in day_types:
        for trig in dt.get("triggers", []):
            if trigger_matches(trig, sig):
                return dt, "trigger"
    for dt in day_types:
        if dt.get("id") == "rest_day":
            return dt, "fallback"
    return None, "none"


def resolve_nutrition(date: str, run_block: dict | None, strength_block: dict | None,
                      day_types: list[dict],
                      date_overrides: dict[str, dict] | None = None) -> dict | None:
    """
    Resolve one day's nutrition block.
    Order (master schema app_daily_view_assembly): date override first; otherwise
    first matching day_type, then its phase adjustment.
    Output keeps the May-2026 shape; new keys are additive:
      source ("date_override" | "day_type"), matched_by ("trigger" | "fallback"),
      override_reason, matched_day_type_label, signature.
    """
    sig = day_signature(run_block, strength_block)
    phase = (run_block or {}).get("phase") or (strength_block or {}).get("phase")

    matched, matched_by = match_day_type(sig, day_types)
    override = (date_overrides or {}).get(date)

    if not matched and not override:
        return None

    base_targets = dict((matched or {}).get("daily_targets", {}))
    block = {
        "day_type_id": (matched or {}).get("id"),
        "day_type_label": (matched or {}).get("label"),
        "phase_adjustment_applied": None,
        "daily_targets": base_targets,
        "session_fueling": (matched or {}).get("session_fueling", []),
        "notes": (matched or {}).get("notes", ""),
        "source": "day_type",
        "matched_by": matched_by,
        "signature": sig,
    }

    if override:
        # The override replaces the day type (schema). Fields it leaves out fall
        # back to the matched day type so the card is never empty.
        block["source"] = "date_override"
        block["override_reason"] = override.get("reason", "")
        block["matched_day_type_label"] = block["day_type_label"]
        block["day_type_label"] = override.get("reason") or block["day_type_label"]
        if "daily_targets" in override:
            merged = dict(base_targets)
            merged.update(override["daily_targets"])
            block["daily_targets"] = merged
        if "session_fueling" in override:
            block["session_fueling"] = override["session_fueling"]
        if override.get("notes"):
            block["notes"] = override["notes"]
        return block

    for adj in matched.get("phase_adjustments", []):
        if adj.get("phase") == phase:
            block["phase_adjustment_applied"] = adj
            targets = dict(base_targets)
            targets.update(adj.get("modified_targets", {}))
            block["daily_targets"] = targets
            break
    return block


def nutrition_source(nutrition_data: dict) -> dict:
    """What the combined file keeps so nutrition can be re-resolved later."""
    return {
        "day_types": nutrition_data.get("day_types", []),
        "date_overrides": nutrition_data.get("date_overrides", []),
    }


def overrides_by_date(source: dict) -> dict[str, dict]:
    return {o["date"]: o for o in source.get("date_overrides", []) if o.get("date")}


def resolve_all_nutrition(days: dict, source: dict) -> list[str]:
    """Re-resolve every day in place. Returns dates with no nutrition."""
    day_types = source.get("day_types", [])
    ovr = overrides_by_date(source)
    unmatched = []
    for d, day in days.items():
        day["nutrition"] = resolve_nutrition(d, day.get("run"), day.get("strength"), day_types, ovr)
        if day["nutrition"] is None:
            unmatched.append(d)
    unused = sorted(set(ovr) - set(days))
    if unused:
        print(f"  WARNING: {len(unused)} nutrition override date(s) are outside the plan: "
              f"{', '.join(unused)}")
    return unmatched


# ---------------------------------------------------------------------------
# Meta, routines
# ---------------------------------------------------------------------------

def source_meta(meta: dict) -> dict:
    return {
        "schema_version": meta.get("schema_version"),
        "revision": meta.get("revision"),
        "generated_by": meta.get("generated_by"),
        "generated_date": meta.get("generated_date"),
    }


def hr_meta(run_meta: dict) -> dict:
    return {k: run_meta[k] for k in HR_META_FIELDS if k in run_meta}


def merge_daily_routines(run_data: dict | None, strength_data: dict | None,
                         existing: list[dict] | None = None,
                         replace: str | None = None) -> list[dict]:
    """
    Daily routines from both plans (master schema step 3), tagged with "owner".
    Strength first (it owns most of them), then run; duplicates by id are dropped.
    With replace="run"/"strength", only that owner's routines are swapped out of
    `existing` (used by update_plan.py).
    """
    def tag(routines, owner):
        return [dict(r, owner=owner) for r in routines or []]

    if replace:
        # Routines in older combined files have no owner tag: they came from strength.
        keep = [r for r in existing or [] if r.get("owner", "strength") != replace]
        new_src = (run_data if replace == "run" else strength_data) or {}
        combined = keep + tag(new_src.get("daily_routines"), replace)
    else:
        combined = tag((strength_data or {}).get("daily_routines"), "strength") + \
                   tag((run_data or {}).get("daily_routines"), "run")

    seen, out = set(), []
    order = {"strength": 0, "run": 1}
    for r in sorted(combined, key=lambda r: order.get(r.get("owner"), 2)):
        rid = r.get("id")
        if rid and rid in seen:
            continue
        seen.add(rid)
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_run_weekly_targets(run_data: dict) -> list[str]:
    """Derived weekly targets vs session sums (race_pace excluded)."""
    issues = []
    for week in run_data.get("weeks", []):
        wt = week.get("weekly_targets") or {}
        mi = up = down = mins = 0.0
        for day in week.get("days", []):
            for s in day.get("sessions", []):
                if s.get("type") != "run" or s.get("subtype") == "race_pace":
                    continue
                mi += s.get("distance_mi", 0) or 0
                gain = s.get("elevation_gain_ft", 0) or 0
                up += gain
                down += s.get("elevation_loss_ft", gain) or 0
                mins += s.get("duration_min", 0) or 0
        w = week.get("week_number")
        checks = [
            ("mileage_mi", mi, TOL_MILES),
            ("vertical_ft", up, TOL_FEET),
            ("descent_ft", down, TOL_FEET),
            ("run_hours_est", mins / 60, TOL_HOURS),
        ]
        for key, actual, tol in checks:
            if key in wt and abs((wt[key] or 0) - actual) > tol:
                issues.append(f"W{w} {key}: target {wt[key]} vs session sum {round(actual, 2)}")
    return issues


def check_strength_weekly_targets(strength_data: dict) -> list[str]:
    issues = []
    for week in strength_data.get("weeks", []):
        wt = week.get("weekly_targets") or {}
        n_str = n_swim = swim_min = 0
        for day in week.get("days", []):
            for s in day.get("sessions", []):
                if s.get("type") == "strength":
                    n_str += 1
                elif s.get("type") == "cross" and s.get("subtype") == "swim":
                    n_swim += 1
                    swim_min += s.get("duration_min", 0) or 0
        w = week.get("week_number")
        for key, actual in (("strength_sessions", n_str), ("swim_sessions", n_swim),
                            ("swim_minutes", swim_min)):
            if key in wt and wt[key] != actual:
                issues.append(f"W{w} {key}: target {wt[key]} vs session count {actual}")
    return issues


def check_calendar(run_days: dict, strength_days: dict) -> list[str]:
    issues = []
    for d in sorted(set(run_days) | set(strength_days)):
        actual = _date.fromisoformat(d).strftime("%A")
        for name, src in (("run", run_days), ("strength", strength_days)):
            day = src.get(d)
            if day and day.get("day_of_week") != actual:
                issues.append(f"{d} ({name}): day_of_week '{day.get('day_of_week')}' but date is {actual}")
        r, s = run_days.get(d), strength_days.get(d)
        if r and s and r.get("week_number") != s.get("week_number"):
            issues.append(f"{d}: run week {r.get('week_number')} vs strength week {s.get('week_number')}")
        if not r:
            issues.append(f"{d}: in strength plan only")
        if not s:
            issues.append(f"{d}: in run plan only")
    return issues


def report(title: str, issues: list[str], limit: int = 30) -> None:
    if not issues:
        print(f"  {title}: ✓")
        return
    print(f"  WARNING — {title}: {len(issues)} issue(s)")
    for i in issues[:limit]:
        print(f"    {i}")
    if len(issues) > limit:
        print(f"    … {len(issues) - limit} more")


def print_nutrition_summary(days: dict) -> None:
    counts = defaultdict(int)
    overrides, fallbacks = [], []
    for d, day in sorted(days.items()):
        nut = day.get("nutrition")
        if not nut:
            continue
        counts[nut["day_type_id"]] += 1
        if nut.get("source") == "date_override":
            overrides.append(d)
        if nut.get("matched_by") == "fallback" and not day.get("rest_day"):
            fallbacks.append(f"{d} {nut.get('signature')}")
    print("\nNutrition day-type distribution:")
    for tid, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {str(tid):40s} {n}")
    print(f"  date overrides applied: {len(overrides)}")
    if fallbacks:
        print(f"\n  WARNING: {len(fallbacks)} training day(s) matched no trigger and fell back to rest_day:")
        for f in fallbacks:
            print(f"    {f}")


# ---------------------------------------------------------------------------
# Main merge
# ---------------------------------------------------------------------------

def merge(run_path: Path, strength_path: Path, nutrition_path: Path,
          schema_path: Path | None = None) -> dict:
    run_data = load_plan(run_path, "run")
    strength_data = load_plan(strength_path, "strength")
    nutrition_data = load_plan(nutrition_path, "nutrition")

    if schema_path:
        validate_against_schema(schema_path, {
            "run": run_data, "strength": strength_data, "nutrition": nutrition_data})

    run_meta, str_meta, nut_meta = run_data["meta"], strength_data["meta"], nutrition_data["meta"]

    meta = {
        "athlete": run_meta.get("athlete"),
        "race": run_meta.get("race"),
        "race_date": run_meta.get("race_date"),
        "plan_start": run_meta.get("plan_start"),
        "plan_end": max(run_meta.get("plan_end", ""), str_meta.get("plan_end", ""),
                        nut_meta.get("plan_end", "")),
        "hr": hr_meta(run_meta),
        "sources": {
            "run": source_meta(run_meta),
            "strength": source_meta(str_meta),
            "nutrition": source_meta(nut_meta),
        },
    }

    run_days = weeks_to_days(run_data.get("weeks", []))
    strength_days = weeks_to_days(strength_data.get("weeks", []))

    print("Checks:")
    report("run weekly targets vs session sums", check_run_weekly_targets(run_data))
    report("strength/swim weekly counts", check_strength_weekly_targets(strength_data))
    report("calendar alignment", check_calendar(run_days, strength_days))

    days = {}
    for d in sorted(set(run_days) | set(strength_days)):
        run_day, str_day = run_days.get(d), strength_days.get(d)
        src = run_day or str_day
        run_block, strength_block = build_block(run_day), build_block(str_day)
        days[d] = {
            "date": d,
            "day_of_week": src.get("day_of_week"),
            "week_number": src.get("week_number"),
            "flex": (run_day or {}).get("flex", (str_day or {}).get("flex", False)),
            "rest_day": compute_rest_day(run_block, strength_block),
            "run": run_block,
            "strength": strength_block,
            "nutrition": None,
        }

    nsrc = nutrition_source(nutrition_data)
    unmatched = resolve_all_nutrition(days, nsrc)
    if unmatched:
        print(f"  WARNING: {len(unmatched)} day(s) have no nutrition: {', '.join(unmatched)}")

    return {
        "meta": meta,
        "session_protocols": {
            "run": run_data.get("session_protocols", {}),
            "strength": strength_data.get("session_protocols", {}),
        },
        "daily_routines": merge_daily_routines(run_data, strength_data),
        "supplements": nutrition_data.get("supplements", []),
        "nutrition_source": nsrc,
        "days": days,
    }


def write_json(path: Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="Merge run / strength / nutrition plan JSON files")
    p.add_argument("--run", required=True, type=Path)
    p.add_argument("--strength", required=True, type=Path)
    p.add_argument("--nutrition", required=True, type=Path)
    p.add_argument("--output-full", required=True, type=Path)
    p.add_argument("--output-week1", type=Path, default=None,
                   help="Optional one-week slice (week chosen by --slice-week)")
    p.add_argument("--slice-week", type=int, default=1)
    p.add_argument("--schema", type=Path, default=None,
                   help="Optional master schema for full validation (needs jsonschema)")
    args = p.parse_args()

    print("Merging plans…")
    combined = merge(args.run, args.strength, args.nutrition, args.schema)

    write_json(args.output_full, combined)
    dates = sorted(combined["days"])
    print(f"\n  Full plan: {len(dates)} days → {args.output_full}")
    print(f"  Range    : {dates[0]} → {dates[-1]}")

    if args.output_week1:
        sl = dict(combined)
        sl["days"] = {d: v for d, v in combined["days"].items()
                      if v.get("week_number") == args.slice_week}
        sl["meta"] = dict(combined["meta"], slice=f"week_{args.slice_week}_only")
        write_json(args.output_week1, sl)
        print(f"  Week {args.slice_week} slice: {len(sl['days'])} days → {args.output_week1}")

    print_nutrition_summary(combined["days"])
    print("\nDone.")


if __name__ == "__main__":
    main()
