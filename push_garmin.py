#!/usr/bin/env python3
"""
push_garmin.py — push the plan's run sessions to Garmin Connect as structured
workouts, scheduled on their dates, so the watch enforces hr_cap_bpm and the
interval structure.

    python3 push_garmin.py --login                      # once: stores Garmin tokens
    python3 push_garmin.py --dry-run                    # show what would be pushed
    python3 push_garmin.py                              # push the next 14 days
    python3 push_garmin.py --from 2026-11-24 --days 7   # a specific window

Reads combined_plan.json (the merged plan the app fetches). Pushes run sessions
only: no swims, no strength, no race (race_pace), no walks (distance_mi 0).
Each step carries a custom HR range 90..hr_cap_bpm; the watch alerts above it.

Idempotent: garmin_sync_state.json maps date -> {workoutId, name, fingerprint}.
A re-run skips unchanged sessions, replaces changed ones (delete + create), and
deletes sessions that left the plan. It only ever deletes workouts whose ids are
in that state file, or — with --prune — tagged workouts on the calendar that the
state file does not know. Garmin deletion cannot be undone.

Dependencies: Python 3 stdlib + garth (pip install --user garth). The Garmin
JSON shapes and endpoints are the ones garmin-connect-mcp uses.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ── Garmin enums ──────────────────────────────────────────────────────────────
SPORT_RUNNING = {"sportTypeId": 1, "sportTypeKey": "running"}
STEP_TYPE = {
    "warmup":   {"stepTypeId": 1, "stepTypeKey": "warmup"},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval"},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery"},
    "repeat":   {"stepTypeId": 6, "stepTypeKey": "repeat"},
}
END_LAP  = {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}
END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time"}
END_DIST = {"conditionTypeId": 3, "conditionTypeKey": "distance"}      # endConditionValue in metres
UNIT_MILE = {"unitId": 4, "unitKey": "mile", "factor": 1609.344}        # how the watch displays it
M_PER_MI = 1609.344
TARGET_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
TARGET_HR   = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"}

HR_FLOOR_BPM = 90          # low edge of the custom range; keeps the low alert quiet on hikes
NAME_MAX = 60
DESC_MAX = 1000
DEFAULT_DAYS = 14
DEFAULT_STATE = "garmin_sync_state.json"
DEFAULT_TOKEN_DIR = os.path.expanduser("~/.garth")

# Cue patterns (rev 2.11 — checked against every session in the cycle; see --dry-run warnings)
RE_OUTDOOR_CLIMB = re.compile(r"After a (\d+)-min .*?warm-up: (\d+) x (\d+) min", re.I | re.S)
RE_TREADMILL_CLIMB = re.compile(r"(\d+) min Z1-2 at \d+%, then ONE (\d+)-min", re.I | re.S)
# Descent skill sessions: "2 x 1.0 mi sustained at -5..-8%" or "1.5 + 1.5 + 1.0 mi sustained at -5..-8%"
RE_DESCENT_EQUAL = re.compile(r"(\d+) x ([\d.]+) mi[^.]*?sustained at -", re.I)
RE_DESCENT_LIST = re.compile(r"((?:[\d.]+ \+ )+[\d.]+) mi sustained at -", re.I)
# Fast-feet days: "FAST-FEET DAY. 10-15 min easy warm-up, then:" + any of these blocks, in cue order
RE_FASTFEET = re.compile(r"^FAST-FEET DAY\. (\d+)(?:-(\d+))? min easy warm-up", re.I)
RE_PICKUPS = re.compile(r"PICK-UPS \(Uphill Athlete\): (\d+) x (\d+) s", re.I)         # flat pick-ups, >= 2 min jog
RE_HILL_SPRINTS = re.compile(r"HILL SPRINTS x(\d+)", re.I)                             # 8-10 s, full 2-3 min walk-down
RE_DOWNHILL = re.compile(r"(\d+) x ([\d.]+) (s|min) FAST DOWNHILL PICK-UPS", re.I)     # jog/walk back up between
FASTFEET_WARMUP_MIN = 15      # the cue says 10-15; take the long end before fast work
PICKUP_REST_SEC = 120         # ">= 2 min easy jog between"
SPRINT_SEC = 10               # "8-10 s uphill"
SPRINT_REST_SEC = 180         # "full 2-3 min walk-down recovery - never cut the rest short"

# Uphill Athlete drift test: 15 warm-up, 30 + 30 at a locked speed (lap at 30:00 / 60:00), 10 down
TIME_TRIAL_STEPS = [("warmup", 15), ("interval", 30), ("interval", 30), ("cooldown", 10)]


def log(msg):
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)


# ── Plan → steps ──────────────────────────────────────────────────────────────
def hr_target(cap):
    return {"targetType": TARGET_HR, "targetValueOne": HR_FLOOR_BPM, "targetValueTwo": int(cap), "zoneNumber": None}


def no_target():
    return {"targetType": TARGET_NONE, "targetValueOne": None, "targetValueTwo": None, "zoneNumber": None}


def step(kind, minutes=None, target=None, *, seconds=None, miles=None):
    """An executable step ending after `minutes` / `seconds` of time, `miles` of distance,
    or — when none is given — on the lap button."""
    if minutes is not None:
        seconds = minutes * 60
    return {"kind": kind, "seconds": seconds, "miles": miles, "target": target if target is not None else no_target()}


def seconds_of(steps):
    """Timed length of a step list (lap-button steps count 0; repeats multiply)."""
    return sum(s["n"] * seconds_of(s["children"]) if s["kind"] == "repeat" else (s["seconds"] or 0) for s in steps)


def fastfeet_blocks(cue, cap):
    """The rep blocks of a fast-feet day (or the sprints tacked onto a climb), in cue order."""
    found = []
    for m in RE_PICKUPS.finditer(cue):
        n, secs = int(m.group(1)), int(m.group(2))
        found.append((m.start(), repeat(n, [step("interval", seconds=secs), step("recovery", seconds=PICKUP_REST_SEC, target=hr_target(cap))])))
    for m in RE_HILL_SPRINTS.finditer(cue):
        found.append((m.start(), repeat(int(m.group(1)), [step("interval", seconds=SPRINT_SEC), step("recovery", seconds=SPRINT_REST_SEC, target=hr_target(cap))])))
    for m in RE_DOWNHILL.finditer(cue):
        n, val, unit = int(m.group(1)), float(m.group(2)), m.group(3).lower()
        secs = int(round(val * 60)) if unit == "min" else int(round(val))
        found.append((m.start(), repeat(n, [step("interval", seconds=secs), step("recovery", target=hr_target(cap))])))  # jog/walk back: lap
    return [blk for _, blk in sorted(found, key=lambda t: t[0])]


def with_cooldown(steps, dur_min, cap, untimed_rest_min=2.0):
    """Append a cooldown for whatever time is left after the structured part.
    Lap-button recoveries are assumed `untimed_rest_min` each for the estimate."""
    def untimed(ss):
        return sum(s["n"] * untimed(s["children"]) if s["kind"] == "repeat" else (s["seconds"] is None and s["miles"] is None) for s in ss)
    left = dur_min - seconds_of(steps) / 60 - untimed(steps) * untimed_rest_min
    steps.append(step("cooldown", round(left) if left >= 5 else None, hr_target(cap)))
    return steps


def repeat(n, children):
    return {"kind": "repeat", "n": n, "children": children}


def plan_steps(session):
    """Return (steps, warning|None) for a run session."""
    sub = session["subtype"]
    dur = session["duration_min"]
    cap = session["hr_cap_bpm"]
    cue = session.get("key_cue", "")

    if sub == "time_trial":
        return [step(k, m) for k, m in TIME_TRIAL_STEPS], None

    if sub == "power_climb":
        m = RE_OUTDOOR_CLIMB.search(cue)
        if m:
            wu, n, work = (int(x) for x in m.groups())
            steps = [step("warmup", wu, hr_target(cap)),
                     repeat(n, [step("interval", work, hr_target(cap)), step("recovery", target=hr_target(cap))])]
            steps += fastfeet_blocks(cue, cap)          # hill sprints tacked onto some climb days
            return with_cooldown(steps, dur, cap), None
        m = RE_TREADMILL_CLIMB.search(cue)
        if m:
            wu, work = (int(x) for x in m.groups())
            steps = [step("warmup", wu, hr_target(cap)), step("interval", work, hr_target(cap))]
            return with_cooldown(steps, dur, cap), None
        return [step("interval", dur, hr_target(cap))], "unparsed climb cue — pushed as a single capped step"

    # Descent skill session: reps by distance, HR audited not governed on the way down,
    # capped on the climb back (lap button — "the ascent is transport"). Warm-up ends on lap.
    m = RE_DESCENT_EQUAL.search(cue)
    reps = None
    if m:
        reps = [float(m.group(2))] * int(m.group(1))
    else:
        m = RE_DESCENT_LIST.search(cue)
        if m:
            reps = [float(x) for x in m.group(1).split(" + ")]
    if reps:
        steps = [step("warmup", target=hr_target(cap))]
        if len(set(reps)) == 1:
            steps.append(repeat(len(reps), [step("interval", miles=reps[0]), step("recovery", target=hr_target(cap))]))
        else:
            for d_mi in reps:
                steps += [step("interval", miles=d_mi), step("recovery", target=hr_target(cap))]
        return steps, None

    # Fast-feet day: warm-up, then pick-ups / hill sprints / downhill pick-ups as the cue lists them
    if RE_FASTFEET.search(cue):
        blocks = fastfeet_blocks(cue, cap)
        if not blocks:
            return [step("interval", dur, hr_target(cap))], "fast-feet cue with no recognisable rep block — pushed as a single capped step"
        steps = [step("warmup", FASTFEET_WARMUP_MIN, hr_target(cap))] + blocks
        return with_cooldown(steps, dur, cap), None

    # easy, long, back_to_back, shakeout, anything else with a duration
    return [step("interval", dur, hr_target(cap))], None


def to_garmin_steps(steps, counter):
    """Flatten our step list into Garmin DTOs, numbering stepId/stepOrder in document order."""
    out = []
    for s in steps:
        counter[0] += 1
        order = counter[0]
        if s["kind"] == "repeat":
            out.append({
                "type": "RepeatGroupDTO", "stepId": order, "stepOrder": order,
                "numberOfIterations": s["n"], "smartRepeat": False, "childStepId": 1,
                "stepType": STEP_TYPE["repeat"],
                "workoutSteps": to_garmin_steps(s["children"], counter),
            })
            continue
        if s["miles"] is not None:
            end, value, unit = END_DIST, round(s["miles"] * M_PER_MI, 1), UNIT_MILE
        elif s["seconds"] is not None:
            end, value, unit = END_TIME, int(round(s["seconds"])), None
        else:
            end, value, unit = END_LAP, 1, None
        out.append({
            "type": "ExecutableStepDTO", "stepId": order, "stepOrder": order,
            "childStepId": None, "description": None,
            "stepType": STEP_TYPE[s["kind"]],
            "endCondition": end,
            "endConditionValue": value,
            "endConditionCompare": None, "endConditionZone": None,
            "preferredEndConditionUnit": unit,
            **s["target"],
            "exerciseName": None, "strokeType": None, "equipmentType": None, "category": None,
        })
    return out


def race_tag(meta):
    race = meta.get("race") or (meta.get("sources", {}).get("run", {}) or {}).get("race") or "PLAN"
    parts = []
    for tok in re.findall(r"[A-Za-z0-9]+", race):
        parts.append(tok.upper() if tok[0].isdigit() else tok[0].upper())
    return "".join(parts)


def workout_name(tag, day, session):
    base = f"{tag} W{int(day['week_number']):02d} {day['day_of_week'][:3]} {session['distance_mi']:g}mi · "
    name = base + session["title"]
    if len(name) > NAME_MAX:                       # trim on a word boundary
        cut = name.rfind(" ", len(base), NAME_MAX)
        name = name[:cut if cut > len(base) else NAME_MAX].rstrip(" -,:(")
    return name


def plan_line(session):
    """One-line load summary: '9.5 mi · +1400/-1400 ft · 111 min · HR cap 147 (Z1-2)'."""
    gain = session.get("elevation_gain_ft", 0)
    loss = session.get("elevation_loss_ft", gain)
    return (f"{session['distance_mi']:g} mi · +{gain:g}/-{loss:g} ft · {session['duration_min']:g} min"
            f" · HR cap {session['hr_cap_bpm']} ({session.get('hr_zone', '')})"
            + (" · poles" if session.get("poles") else ""))


def workout_description(session):
    desc = "PLAN: " + plan_line(session) + "\n\n" + session.get("key_cue", "").strip()
    if session.get("fueling_notes"):
        desc += "\n\nFUEL: " + session["fueling_notes"].strip()
    return desc[:DESC_MAX]


def build_payload(tag, day, session):
    steps, warning = plan_steps(session)
    payload = {
        "workoutName": workout_name(tag, day, session),
        "description": workout_description(session),
        "sportType": SPORT_RUNNING,
        "subSportType": None,
        "workoutSegments": [{"segmentOrder": 1, "sportType": SPORT_RUNNING, "workoutSteps": to_garmin_steps(steps, [0])}],
        # Planned load, shown as "estimated" on the workout card; the steps still end by time.
        "estimatedDistanceUnit": {"unitKey": "mile"},
        "avgTrainingSpeed": 3,
        "estimatedDurationInSecs": int(round(session["duration_min"] * 60)),
        "estimatedDistanceInMeters": int(round(session["distance_mi"] * M_PER_MI)),
        "estimateType": "TIME_ESTIMATED",
        "isWheelchair": False,
    }
    return payload, steps, warning


def describe_steps(steps, indent="    "):
    lines = []
    for s in steps:
        if s["kind"] == "repeat":
            lines.append(f"{indent}{s['n']} x [")
            lines += describe_steps(s["children"], indent + "    ")
            lines.append(f"{indent}]")
            continue
        if s["miles"] is not None:
            end = f"{s['miles']:g} mi"
        elif s["seconds"] is None:
            end = "lap button"
        elif s["seconds"] % 60 == 0:
            end = f"{int(s['seconds'] // 60)} min"
        else:
            end = f"{int(s['seconds'])} s"
        t = s["target"]
        tgt = f"HR {t['targetValueOne']}-{t['targetValueTwo']}" if t["targetType"] is TARGET_HR else "no target"
        lines.append(f"{indent}{s['kind']:<9} {end:<11} {tgt}")
    return lines


def pushable(session):
    return (
        session.get("type") == "run"
        and session.get("subtype") != "race_pace"
        and (session.get("distance_mi") or 0) > 0
        and session.get("duration_min")
        and session.get("hr_cap_bpm")
    )


def sessions_in_window(plan, start, days):
    """Yield (date_str, day, session) for pushable run sessions in [start, start+days)."""
    end = start + dt.timedelta(days=days)
    for date_str in sorted(plan["days"]):
        d = dt.date.fromisoformat(date_str)
        if not (start <= d < end):
            continue
        day = plan["days"][date_str]
        runs = [s for s in ((day.get("run") or {}).get("sessions") or []) if pushable(s)]
        if len(runs) > 1:
            log(f"WARNING {date_str}: {len(runs)} pushable run sessions; pushing the first only")
        if runs:
            yield date_str, day, runs[0]


def fingerprint(date_str, payload):
    return hashlib.sha1((date_str + json.dumps(payload, sort_keys=True)).encode()).hexdigest()


# ── Garmin ────────────────────────────────────────────────────────────────────
def garmin():
    import garth
    return garth


def gc_login(token_dir, email):
    """Interactive one-time login via garmin_login.mjs (the npm garmin-connect library).

    garth's own login hits Garmin's mobile endpoint, which now returns 429 to scripts;
    the Node library signs in through the web SSO and we store its tokens in garth's
    format. Needs node + npx (no project install: npx fetches garmin-connect)."""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    email = email or os.environ.get("GARMIN_USERNAME") or input("Garmin email: ").strip()
    inner = f'node "{os.path.join(here, "garmin_login.mjs")}" --dir "{token_dir}" --email "{email}"'
    cmd = ["npx", "-y", "-p", "garmin-connect@1.6.2", "-c", inner]
    rc = subprocess.call(cmd)
    if rc != 0:
        sys.exit(f"login failed (exit {rc})")
    gc_resume(token_dir)              # proves garth can read what was written
    log(f"garth resumed from {token_dir}: OK")


def gc_resume(token_dir):
    g = garmin()
    if not os.path.isdir(token_dir):
        sys.exit(f"No Garmin tokens in {token_dir}. Run: python3 push_garmin.py --login")
    g.resume(token_dir)
    try:
        g.save(token_dir)             # persist a refreshed OAuth2 token
    except Exception:
        pass
    return g


def gc_create(g, payload):
    resp = g.connectapi("/workout-service/workout", method="POST", json=payload)
    return int(resp["workoutId"])


def gc_schedule(g, workout_id, date_str):
    g.connectapi(f"/workout-service/schedule/{workout_id}", method="POST", json={"date": date_str})


def gc_delete(g, workout_id):
    try:
        g.connectapi(f"/workout-service/workout/{workout_id}", method="DELETE")
    except Exception as e:                      # already gone -> nothing to do
        if "404" not in str(e):
            raise


def gc_calendar_workouts(g, start, days):
    """Tagged-or-not workouts on the calendar across the months the window touches."""
    end = start + dt.timedelta(days=days - 1)
    months = {(start.year, start.month), (end.year, end.month)}
    items = []
    for y, m in sorted(months):
        data = g.connectapi(f"/calendar-service/year/{y}/month/{m - 1}") or {}   # Garmin months are 0-based
        for it in data.get("calendarItems", []):
            if it.get("itemType") == "workout" and it.get("workoutId"):
                items.append({"date": it.get("date"), "workoutId": int(it["workoutId"]), "title": it.get("title") or ""})
    return items


# ── State ─────────────────────────────────────────────────────────────────────
def load_state(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", default="combined_plan.json")
    ap.add_argument("--from", dest="start", help="first date, YYYY-MM-DD (default today)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--tag", help="workout-name prefix (default: initials of meta.race, e.g. TC103K)")
    ap.add_argument("--dry-run", action="store_true", help="print the workouts; no network, no state writes")
    ap.add_argument("--json", action="store_true", help="with --dry-run, also print each Garmin payload")
    ap.add_argument("--prune", action="store_true", help="delete tagged calendar workouts the state file does not know")
    ap.add_argument("--login", action="store_true", help="interactive one-time login; stores tokens in --token-dir")
    ap.add_argument("--email")
    ap.add_argument("--token-dir", default=DEFAULT_TOKEN_DIR)
    args = ap.parse_args()

    if args.login:
        gc_login(args.token_dir, args.email)
        return

    with open(args.plan) as f:
        plan = json.load(f)
    tag = args.tag or race_tag(plan.get("meta", {}))
    start = dt.date.fromisoformat(args.start) if args.start else dt.date.today()
    rev = (plan.get("meta", {}).get("sources", {}).get("run", {}) or {}).get("revision", "?")
    log(f"plan {args.plan} (run rev {rev}), tag {tag}, window {start} +{args.days}d, state {args.state}"
        + (" [DRY RUN]" if args.dry_run else ""))

    wanted = {}
    for date_str, day, session in sessions_in_window(plan, start, args.days):
        payload, steps, warning = build_payload(tag, day, session)
        wanted[date_str] = (payload, steps, warning)

    if args.dry_run:
        for date_str, (payload, steps, warning) in wanted.items():
            print(f"\n{date_str}  {payload['workoutName']}")
            print("\n".join(describe_steps(steps)))
            if warning:
                print(f"    WARNING: {warning}")
            if args.json:
                print(json.dumps(payload, indent=1))
        print(f"\n{len(wanted)} workout(s) in window; nothing pushed.")
        return

    # One run at a time: launchd fires on a timer AND on plan-file changes, and two
    # concurrent runs would both create a workout for the same date.
    lock = open(args.state + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another push is running; waiting for it")
        fcntl.flock(lock, fcntl.LOCK_EX)

    g = gc_resume(args.token_dir)
    state = load_state(args.state)
    summary = []

    # Dates in the window that are in state but no longer wanted -> delete
    end = start + dt.timedelta(days=args.days)
    for date_str in sorted(state):
        d = dt.date.fromisoformat(date_str)
        if start <= d < end and date_str not in wanted:
            gc_delete(g, state[date_str]["workoutId"])
            summary.append((date_str, "deleted", state[date_str]["name"]))
            del state[date_str]
            save_state(args.state, state)

    for date_str, (payload, steps, warning) in wanted.items():
        fp = fingerprint(date_str, payload)
        prev = state.get(date_str)
        if prev and prev.get("fingerprint") == fp:
            summary.append((date_str, "skipped", prev["name"]))
            continue
        if prev:
            gc_delete(g, prev["workoutId"])
        wid = gc_create(g, payload)
        gc_schedule(g, wid, date_str)
        state[date_str] = {"workoutId": wid, "name": payload["workoutName"], "fingerprint": fp}
        save_state(args.state, state)
        summary.append((date_str, "updated" if prev else "created", payload["workoutName"]))
        if warning:
            log(f"WARNING {date_str}: {warning}")

    # Reconcile: tagged workouts on the calendar that state does not know
    known = {v["workoutId"] for v in state.values()}
    orphans = [it for it in gc_calendar_workouts(g, start, args.days)
               if it["title"].startswith(tag + " ") and it["workoutId"] not in known
               and it["date"] and start <= dt.date.fromisoformat(it["date"]) < end]
    for it in orphans:
        if args.prune:
            gc_delete(g, it["workoutId"])
            summary.append((it["date"], "pruned", it["title"]))
        else:
            log(f"WARNING orphan on calendar {it['date']}: '{it['title']}' (workoutId {it['workoutId']}) — re-run with --prune to delete")

    for date_str, action, name in sorted(summary):
        print(f"  {date_str}  {action:<8} {name}")
    counts = {}
    for _, action, _ in summary:
        counts[action] = counts.get(action, 0) + 1
    log("done: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) if counts else "done: nothing to do")


if __name__ == "__main__":
    main()
