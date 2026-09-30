"""
merge_plans.py — Merges run_plan.json, strength_plan.json, and nutrition_plan.json
into a unified daily-keyed JSON file.

Usage:
    python merge_plans.py \
        --run run_plan.json \
        --strength strength_plan.json \
        --nutrition nutrition_plan.json \
        --output-full combined_plan.json \
        --output-week1 week1_plan.json \
        [--schema master_schema_v6.json]

Assumptions about source files (validated on load):
  - run / strength: top-level keys include "meta", "weeks" (array of week objects),
    "session_protocols", "daily_routines"
  - nutrition: top-level keys include "meta", "day_types" (array), "supplements"
  - All dates are ISO 8601 YYYY-MM-DD strings

Schema v1.7 (master_schema_v6.json) notes:
  - day.sessions items are run | strength | cross, discriminated on session.type.
    The strength plan carries the swims (type "cross", subtype "swim").
  - Nutrition triggers match on (run_subtype, strength_subtype) and, only when the
    trigger has the key, cross_subtype — see trigger_matches().
  - rest_day is per-plan. The merged day is a rest day only if NO plan has a
    session on it (a run-plan rest Monday can carry a strength-plan swim).
  - Schema validation is reported, never fatal. It uses the jsonschema package
    when it is installed and a built-in Draft-07 subset otherwise, so the script
    stays stdlib-only for CI and the pre-commit hook.
"""

import json
import re
import argparse
from pathlib import Path
from collections import defaultdict


DEFAULT_SCHEMA = Path(__file__).resolve().parent / "master_schema_v6.json"

# Order of sessions within a day (spec 3.4): run (AM) -> cross (swim) -> strength
# (PM). The strength file lists the swim before the lift, and the two plans are
# separate files, so file order cannot be relied on across them.
SESSION_ORDER = {"run": 0, "cross": 1, "strength": 2}

# Loader assertion tolerances (spec 2.5).
MILEAGE_TOL_MI = 0.5
VERTICAL_TOL_FT = 50
# Run subtypes excluded from weekly sums — races are not training load targets.
EXCLUDED_FROM_SUMS = {"race_pace"}

# meta fields copied from the run plan into the merged meta (spec 2.7).
PHYSIOLOGY_FIELDS = (
    "lthr_bpm", "lthr_confidence", "lthr_source", "lthr_updated",
    "aet_bpm", "aet_status", "aet_gate_bpm", "aet_test_dates",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_week_range(s: str) -> tuple[int, int]:
    """Parse "1-13" → (1, 13). Also handles bare "7" → (7, 7)."""
    parts = s.split("-")
    if len(parts) == 2:
        return int(parts[0]), int(parts[1])
    return int(parts[0]), int(parts[0])


def revision_key(rev) -> tuple:
    """Sort key for revision strings, compared component-wise.

    "2.10" is newer than "2.9", though it sorts before it as text. A component
    with a letter suffix ("1.4a") sorts after the bare number and before the next
    one. None / empty sorts before everything.
    """
    if not rev:
        return ()
    key = []
    for part in str(rev).split("."):
        m = re.match(r"^(\d*)(.*)$", part)
        num, suffix = m.group(1), m.group(2)
        key.append((int(num) if num else -1, suffix))
    return tuple(key)


def weeks_to_days(weeks: list[dict], plan_type: str = "") -> dict[str, dict]:
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


def day_sessions(*blocks) -> list[dict]:
    """All sessions from the given day/plan blocks, in run -> cross -> strength order.

    sorted() is stable, so sessions of the same type keep their file order.
    """
    sessions = [s for b in blocks if b for s in (b.get("sessions") or [])]
    return sorted(sessions, key=lambda s: SESSION_ORDER.get(s.get("type"), 99))


def is_rest_day(*blocks) -> bool:
    """A day is a rest day only if no plan has a session on it (spec 3.3).

    The per-plan rest_day flag is deliberately ignored: the run plan marks
    swim Mondays rest_day:true while the strength plan puts a swim there.
    """
    return not day_sessions(*blocks)


def day_subtypes(*blocks) -> tuple:
    """(run_subtype, strength_subtype, {cross subtypes}) for a day's sessions.

    r and st are the subtype of the day's FIRST session of that type (None if
    there is none); cr is the SET of cross subtypes, possibly empty.
    """
    sessions = day_sessions(*blocks)
    first = lambda t: next((s.get("subtype") for s in sessions if s.get("type") == t), None)
    cross = {s.get("subtype") for s in sessions if s.get("type") == "cross"}
    return first("run"), first("strength"), cross


def trigger_matches(trigger: dict, run_subtype, strength_subtype, cross_subtypes: set) -> bool:
    """Schema v1.7 trigger rule (spec 4.2).

    run_subtype and strength_subtype must match exactly (JSON null == "no session
    of that type"). cross_subtype only constrains the match when the trigger HAS
    the key — a trigger without it ignores cross sessions, which is why the swim
    day_types sit first in the file.
    """
    if trigger.get("run_subtype") != run_subtype:
        return False
    if trigger.get("strength_subtype") != strength_subtype:
        return False
    if "cross_subtype" in trigger and trigger["cross_subtype"] not in cross_subtypes:
        return False
    return True


def resolve_nutrition(date: str, run_day: dict | None, strength_day: dict | None,
                      day_types: list[dict]) -> dict | None:
    """
    Match a day to a nutrition day_type template.
    Returns the resolved nutrition block or None.
    """
    run_subtype, strength_subtype, cross_subtypes = day_subtypes(run_day, strength_day)

    # Phase for adjustment lookup — the run plan week that contains the date
    # (spec 4.6); strength weeks carry the same phase, so they are the fallback.
    phase = None
    if run_day:
        phase = run_day.get("phase")
    elif strength_day:
        phase = strength_day.get("phase")

    # day_types in file order, then triggers in order; first match wins.
    matched = None
    for dt in day_types:
        if any(trigger_matches(t, run_subtype, strength_subtype, cross_subtypes)
               for t in dt.get("triggers", [])):
            matched = dt
            break

    # Fallback to rest_day if nothing matched.
    #
    # This is safe for a genuinely empty day, but on a day that HAS sessions it
    # silently assigns the lowest-carb template in the plan — the failure that
    # left race day (run_subtype "race_pace", which no trigger covered) sitting
    # on rest-day targets. The fallback is recorded so callers can flag it
    # rather than having it disappear into a plausible-looking block.
    unmatched_fallback = False
    if not matched:
        for dt in day_types:
            if dt.get("id") == "rest_day":
                matched = dt
                unmatched_fallback = True
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

    block = {
        "day_type_id": matched.get("id"),
        "day_type_label": matched.get("label"),
        "phase_adjustment_applied": phase_adjustment_applied,
        "daily_targets": daily_targets,
        "session_fueling": matched.get("session_fueling", []),
        "notes": matched.get("notes", ""),
    }
    if unmatched_fallback and (run_subtype or strength_subtype or cross_subtypes):
        block["unmatched_pairing"] = {
            "run_subtype": run_subtype,
            "strength_subtype": strength_subtype,
            "cross_subtypes": sorted(cross_subtypes),
        }
    return block


def apply_date_override(block: dict | None, override: dict) -> dict:
    """Apply a nutrition date_override on top of a resolved day block.

    date_overrides are the most specific layer in the plan — they exist for days
    the day_type triggers cannot express (race day, carb loads, AeT tests, gate
    runs), so they win over both the template and any phase adjustment.

    Every override in the plan carries a COMPLETE daily_targets, so targets are
    replaced wholesale rather than merged — a partial merge would silently blend
    two different intents. session_fueling is replaced whenever the override has
    the key, INCLUDING an explicit empty array (the carb-load days): the override
    is the answer for that date. Only an absent key keeps the template's entries.

    day_type_id is left alone so the underlying classification stays visible,
    and day_type_label is taken from the override reason.
    """
    block = dict(block or {})
    block["daily_targets"] = dict(override["daily_targets"])
    if "session_fueling" in override:
        block["session_fueling"] = override["session_fueling"]
    block["day_type_label"] = override.get("reason", block.get("day_type_label"))
    block["date_override_applied"] = {
        "date": override.get("date"),
        "reason": override.get("reason"),
    }
    if "notes" in override:
        block["notes"] = override["notes"]
    # An explicit override is a deliberate answer for this date, so a fallback
    # that landed here is no longer an unresolved question.
    block.pop("unmatched_pairing", None)
    return block


def build_plan_block(day: dict | None) -> dict | None:
    """The run / strength namespace of a merged day."""
    if not day:
        return None
    return {
        "phase": day.get("phase"),
        "weekly_targets": day.get("weekly_targets"),
        "week_notes": day.get("week_notes", ""),
        "day_notes": day.get("day_notes", ""),
        "rest_day": day.get("rest_day", False),   # this plan's own flag, informational
        "sessions": day.get("sessions", []),
    }


def build_nutrition(date: str, run_block, strength_block, day_types, overrides) -> dict | None:
    """Nutrition for one date: date_override first (step 5), else day_types (6-7)."""
    block = resolve_nutrition(date, run_block, strength_block, day_types)
    if date in overrides:
        block = apply_date_override(block, overrides[date])
    return block


def source_entry(meta: dict) -> dict:
    """What meta.sources.<plan> records about one source file.

    revision is included so a patch that keeps generated_date (e.g. run rev 2.0
    -> 2.1 on the same day) still registers as a change in the app's update check.
    daily_routines records which routine ids came from this file, so
    update_plan.py can replace one plan's routines without touching the other's.
    """
    return {
        "schema_version": meta.get("schema_version"),
        "revision": meta.get("revision"),
        "generated_by": meta.get("generated_by"),
        "generated_date": meta.get("generated_date"),
        "plan_end": meta.get("plan_end"),
    }


def plan_source_entry(data: dict) -> dict:
    """source_entry() plus the routine ids a run / strength file contributes."""
    entry = source_entry(data.get("meta", {}))
    entry["daily_routines"] = [r.get("id") for r in data.get("daily_routines", [])]
    return entry


def build_meta(run_meta: dict, sources: dict) -> dict:
    """Merged meta. Race identity and physiology come from the run plan."""
    plan_end = max((s.get("plan_end") or "") for s in sources.values())
    meta = {
        "athlete": run_meta.get("athlete"),
        "race": run_meta.get("race"),
        "race_date": run_meta.get("race_date"),
        "plan_start": run_meta.get("plan_start"),
        "plan_end": plan_end,
        "physiology": {k: run_meta[k] for k in PHYSIOLOGY_FIELDS if k in run_meta},
        "sources": sources,
    }
    return meta


def merge_routines(run_routines: list[dict], strength_routines: list[dict]) -> list[dict]:
    """daily_routines from run + strength (schema step 3). Strength wins on an id clash.

    Kept a flat list (not namespaced like session_protocols) so an installed copy
    of the previous app, which reads plan.daily_routines as an array, keeps working
    against a new combined_plan.json until the service worker update is applied.
    """
    by_id = {}
    for r in run_routines + strength_routines:
        by_id[r.get("id")] = r
    return list(by_id.values())


# ---------------------------------------------------------------------------
# Checks (reported, never auto-corrected)
# ---------------------------------------------------------------------------

def week_checks(run_data: dict, strength_data: dict) -> list[str]:
    """Derived weekly_targets vs the week's session sums (spec 2.5, 3.2, 6.4).

    Run weeks: mileage_mi / vertical_ft equal the sum over run sessions, race_pace
    excluded, within ±0.5 mi / ±50 ft. Cross sessions never count.
    Strength weeks: strength_sessions counts type "strength" only; swim_sessions
    and swim_minutes count cross/swim sessions.
    """
    problems = []
    for w in run_data.get("weeks", []):
        t = w.get("weekly_targets") or {}
        runs = [s for d in w.get("days", []) for s in d.get("sessions", [])
                if s.get("type") == "run" and s.get("subtype") not in EXCLUDED_FROM_SUMS]
        mi = sum(s.get("distance_mi") or 0 for s in runs)
        ft = sum(s.get("elevation_gain_ft") or 0 for s in runs)
        if "mileage_mi" in t and abs(t["mileage_mi"] - mi) > MILEAGE_TOL_MI:
            problems.append(f"run W{w.get('week_number')}: mileage_mi {t['mileage_mi']} != sessions {mi:.1f}")
        if "vertical_ft" in t and abs(t["vertical_ft"] - ft) > VERTICAL_TOL_FT:
            problems.append(f"run W{w.get('week_number')}: vertical_ft {t['vertical_ft']} != sessions {ft:.0f}")
    for w in strength_data.get("weeks", []):
        t = w.get("weekly_targets") or {}
        sessions = [s for d in w.get("days", []) for s in d.get("sessions", [])]
        n_str = sum(1 for s in sessions if s.get("type") == "strength")
        swims = [s for s in sessions if s.get("type") == "cross" and s.get("subtype") == "swim"]
        n_swim = len(swims)
        swim_min = sum(s.get("duration_min") or 0 for s in swims)
        wn = w.get("week_number")
        if "strength_sessions" in t and t["strength_sessions"] != n_str:
            problems.append(f"strength W{wn}: strength_sessions {t['strength_sessions']} != sessions {n_str}")
        if "swim_sessions" in t and t["swim_sessions"] != n_swim:
            problems.append(f"strength W{wn}: swim_sessions {t['swim_sessions']} != sessions {n_swim}")
        if "swim_minutes" in t and t["swim_minutes"] != swim_min:
            problems.append(f"strength W{wn}: swim_minutes {t['swim_minutes']} != sessions {swim_min}")
    return problems


_JSON_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _type_ok(value, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _JSON_TYPES[t])


def _mini_validate(inst, schema: dict, root: dict, path: str, errors: list) -> None:
    """Draft-07 subset — the keywords master_schema_v6.json actually uses."""
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        _mini_validate(inst, node, root, path, errors)
        return
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(inst, t) for t in types):
            errors.append(f"{path}: {inst!r:.60} is not of type {schema['type']}")
            return
    if "const" in schema and inst != schema["const"]:
        errors.append(f"{path}: {inst!r:.60} != const {schema['const']!r}")
    if "enum" in schema and inst not in schema["enum"]:
        errors.append(f"{path}: {inst!r:.60} is not one of {schema['enum']}")
    if isinstance(inst, str):
        if "pattern" in schema and not re.search(schema["pattern"], inst):
            errors.append(f"{path}: {inst!r} does not match {schema['pattern']}")
        if schema.get("format") == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", inst):
            errors.append(f"{path}: {inst!r} is not a date")
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in schema and inst < schema["minimum"]:
            errors.append(f"{path}: {inst} < minimum {schema['minimum']}")
    if isinstance(inst, dict):
        for k in schema.get("required", []):
            if k not in inst:
                errors.append(f"{path}: missing required '{k}'")
        for k, sub in schema.get("properties", {}).items():
            if k in inst:
                _mini_validate(inst[k], sub, root, f"{path}.{k}", errors)
    if isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]:
            errors.append(f"{path}: {len(inst)} items < minItems {schema['minItems']}")
        if "maxItems" in schema and len(inst) > schema["maxItems"]:
            errors.append(f"{path}: {len(inst)} items > maxItems {schema['maxItems']}")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(inst):
                _mini_validate(item, schema["items"], root, f"{path}[{i}]", errors)
    if "oneOf" in schema:
        passing = 0
        for sub in schema["oneOf"]:
            sub_errors = []
            _mini_validate(inst, sub, root, path, sub_errors)
            passing += not sub_errors
        if passing != 1:
            errors.append(f"{path}: matches {passing} of the oneOf branches (needs exactly 1)")
    for sub in schema.get("allOf", []):
        _mini_validate(inst, sub, root, path, errors)
    if "if" in schema:
        probe = []
        _mini_validate(inst, schema["if"], root, path, probe)
        branch = schema.get("then") if not probe else schema.get("else")
        if branch:
            _mini_validate(inst, branch, root, path, errors)


def validate(data: dict, schema: dict) -> list[str]:
    """Schema errors as strings; [] means valid."""
    try:
        import jsonschema
    except ImportError:
        errors = []
        _mini_validate(data, schema, schema, "$", errors)
        return errors
    v = jsonschema.Draft7Validator(schema, format_checker=jsonschema.Draft7Validator.FORMAT_CHECKER)
    return [f"$.{'.'.join(str(p) for p in e.path)}: {e.message[:200]}" for e in v.iter_errors(data)]


# ---------------------------------------------------------------------------
# Main merge logic
# ---------------------------------------------------------------------------

def merge(run_path: Path, strength_path: Path, nutrition_path: Path) -> dict:
    run_data = json.loads(run_path.read_text())
    strength_data = json.loads(strength_path.read_text())
    nutrition_data = json.loads(nutrition_path.read_text())

    meta = build_meta(run_data["meta"], {
        "run": plan_source_entry(run_data),
        "strength": plan_source_entry(strength_data),
        "nutrition": source_entry(nutrition_data["meta"]),
    })

    # --- Session protocols (namespaced) ---
    # strength keys: pre_lower / pre_upper / post_lower / post_upper, selected by
    # strength_session.warmup_protocol (spec 3.5).
    session_protocols = {
        "run": run_data.get("session_protocols", {}),
        "strength": strength_data.get("session_protocols", {}),
    }

    daily_routines = merge_routines(run_data.get("daily_routines", []),
                                    strength_data.get("daily_routines", []))

    # --- Supplements (owned by nutrition) ---
    supplements = nutrition_data.get("supplements", [])

    # --- Flatten weeks → day dicts ---
    run_days = weeks_to_days(run_data.get("weeks", []), "run")
    strength_days = weeks_to_days(strength_data.get("weeks", []), "strength")
    day_types = nutrition_data.get("day_types", [])
    date_overrides = {o["date"]: o for o in nutrition_data.get("date_overrides", [])}

    # --- Collect all dates ---
    all_dates = sorted(set(run_days.keys()) | set(strength_days.keys()))

    # --- Build merged days ---
    days = {}
    for date in all_dates:
        run_day = run_days.get(date)
        str_day = strength_days.get(date)

        # Shared calendar fields — run plan is primary source
        source = run_day or str_day
        flex = (run_day or {}).get("flex", (str_day or {}).get("flex", False))

        days[date] = {
            "date": date,
            "day_of_week": source.get("day_of_week"),
            "week_number": source.get("week_number"),
            "flex": flex,
            "rest_day": is_rest_day(run_day, str_day),
            "run": build_plan_block(run_day),
            "strength": build_plan_block(str_day),
            "nutrition": build_nutrition(date, run_day, str_day, day_types, date_overrides),
        }

    return {
        "meta": meta,
        "session_protocols": session_protocols,
        "daily_routines": daily_routines,
        "supplements": supplements,
        "days": days,
    }


# ---------------------------------------------------------------------------
# Reporting (shared with update_plan.py)
# ---------------------------------------------------------------------------

def print_schema_report(files: dict[str, dict], schema_path: Path | None) -> None:
    if not schema_path:
        return
    if not schema_path.is_file():
        print(f"\nSchema validation skipped: {schema_path} not found")
        return
    schema = json.loads(schema_path.read_text())
    print(f"\nSchema validation ({schema_path.name}, v{schema.get('version')}):")
    for name, data in files.items():
        errors = validate(data, schema)
        print(f"  {name:10s} {'OK' if not errors else f'{len(errors)} ERROR(S)'}")
        for e in errors[:10]:
            print(f"    {e}")
        if len(errors) > 10:
            print(f"    … {len(errors) - 10} more")


def print_week_checks(run_data: dict, strength_data: dict) -> None:
    problems = week_checks(run_data, strength_data)
    if problems:
        print(f"\n  WARNING: {len(problems)} weekly_targets do not match their sessions:")
        for p in problems:
            print(f"    {p}")
    else:
        print("\nWeekly targets vs session sums: all weeks ✓")


def nutrition_distribution(days: dict) -> dict[str, int]:
    """day_type_id counts, with override days counted as OVERRIDE (spec 6.2)."""
    counts = defaultdict(int)
    for day in days.values():
        nut = day.get("nutrition")
        if nut:
            counts["OVERRIDE" if nut.get("date_override_applied") else nut["day_type_id"]] += 1
    return counts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Merge training plan JSON files")
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--strength", required=True, type=Path)
    parser.add_argument("--nutrition", required=True, type=Path)
    parser.add_argument("--output-full", required=True, type=Path)
    parser.add_argument("--output-week1", required=True, type=Path)
    # str, not Path, so --schema '' can switch validation off.
    parser.add_argument("--schema", type=str, default=str(DEFAULT_SCHEMA),
                        help="JSON schema to validate the sources against "
                             "(default: master_schema_v6.json next to this script; '' to skip)")
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

    for name, src in combined["meta"]["sources"].items():
        print(f"  {name:10s} rev {src.get('revision') or '?':6s} schema {src.get('schema_version')}")

    sources = {
        "run": json.loads(args.run.read_text()),
        "strength": json.loads(args.strength.read_text()),
        "nutrition": json.loads(args.nutrition.read_text()),
    }
    print_schema_report(sources, Path(args.schema) if args.schema.strip() else None)

    # Print a quick summary table
    print("\nDate coverage summary:")
    dates = sorted(combined["days"].keys())
    print(f"  First date : {dates[0]}")
    print(f"  Last date  : {dates[-1]}")
    print(f"  Total days : {len(dates)}")
    print(f"  Rest days  : {sum(1 for d in combined['days'].values() if d['rest_day'])} (no session in any plan)")

    # Validate: check every day has a nutrition block
    missing_nutrition = [d for d, v in combined["days"].items() if v["nutrition"] is None]
    if missing_nutrition:
        print(f"\n  WARNING: {len(missing_nutrition)} days have no matched nutrition template:")
        for d in missing_nutrition:
            print(f"    {d}")
    else:
        print("  Nutrition matched: all days ✓")

    print_week_checks(sources["run"], sources["strength"])

    # Report date_overrides. An override naming a date outside the plan window is
    # a silent no-op otherwise — the usual cause is a typo or a shifted race date.
    overrides = {o["date"]: o for o in sources["nutrition"].get("date_overrides", [])}
    applied = [d for d, v in combined["days"].items() if (v["nutrition"] or {}).get("date_override_applied")]
    if overrides:
        print(f"\nNutrition date_overrides: {len(applied)}/{len(overrides)} applied")
        for d in sorted(applied):
            reason = combined["days"][d]["nutrition"]["date_override_applied"]["reason"]
            carbs = combined["days"][d]["nutrition"]["daily_targets"].get("carbs_g")
            print(f"    {d}  {str(carbs) + 'g':<7} {reason[:58]}")
        orphaned = sorted(set(overrides) - set(applied))
        if orphaned:
            print(f"  WARNING: {len(orphaned)} override(s) name a date not in the plan and were IGNORED:")
            for d in orphaned:
                print(f"    {d}  {overrides[d].get('reason','')[:58]}")

    # A day with real sessions that fell back to rest_day is almost always a
    # missing trigger, not a real rest day — it hands a training day the
    # lowest-carb targets in the plan and looks perfectly normal downstream.
    unmatched = sorted(
        (d, v["nutrition"]["unmatched_pairing"])
        for d, v in combined["days"].items()
        if (v["nutrition"] or {}).get("unmatched_pairing")
    )
    if unmatched:
        print(f"\n  WARNING: {len(unmatched)} day(s) with sessions matched NO nutrition trigger")
        print("  and fell back to rest_day targets. Add a trigger, or a date_override:")
        for d, pair in unmatched:
            print(f"    {d}  run={pair['run_subtype']} strength={pair['strength_subtype']} "
                  f"cross={pair['cross_subtypes']}")

    print("\nNutrition day-type distribution (override days counted as OVERRIDE):")
    for tid, count in sorted(nutrition_distribution(combined["days"]).items(), key=lambda x: (-x[1], x[0])):
        print(f"  {tid:40s} {count}")

    print("\nDone.")


if __name__ == "__main__":
    main()
